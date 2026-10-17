use std::{
    collections::HashMap,
    fs,
    io,
    path::{Path, PathBuf},
    time::Duration,
};

use anyhow::{anyhow, Context, Result};
use argh::FromArgs;
use compact_str::CompactString;
use futures::{stream::SelectAll, StreamExt};
use tokio::{
    signal::unix::{signal, SignalKind},
    sync::{broadcast, mpsc},
};
use tokio_stream::wrappers::SignalStream;
use tracing::{error, info, trace, warn};
use tracing_subscriber::{fmt::format::FmtSpan, EnvFilter};

use faasrail_loadgen::{
    sink::{backend::Http as HttpSink, SinkClient},
    source::{backend::Http as HttpSource, backend::RequestSpec, MinuteRange, Poisson, SourceClient},
};

const DEFAULT_MINIO_HOSTPORT: &str = "icy1.cslab.ece.ntua.gr:59000";
const DEFAULT_MINIO_BUCKET_NAME: &str = "snaplace-fbpml";

#[derive(Debug, FromArgs)]
/// faasrail-loadgen-http - HTTP load generator
struct Cli {
    /// path to input CSV file
    #[argh(option)]
    csv: PathBuf,

    /// path to SinkClient's output file
    #[argh(option, short = 'o')]
    outfile: String,

    /// u64 to seed PRNGs (default: system entropy)
    #[argh(option)]
    seed: Option<u64>,

    /// start of invocation ID range (default: 0)
    #[argh(option, default = "0")]
    invoc_id: u64,

    /// subset of input minutes to execute (default: all)
    #[argh(option, default = "MinuteRange::default()")]
    minutes: MinuteRange,

    /// HOST:PORT formatted address of MinIO server
    #[argh(option, default = "String::from(DEFAULT_MINIO_HOSTPORT)")]
    minio_address: String,

    /// name of the MinIO bucket
    #[argh(option, default = "String::from(DEFAULT_MINIO_BUCKET_NAME)")]
    minio_bucket: String,

    /// path to JSON file mapping bench name -> URL/headers
    #[argh(option)]
    request_map: PathBuf,

    /// per-request timeout in seconds (default: 60)
    #[argh(option, default = "60")]
    request_timeout_secs: u64,
}

fn setup_signals_handler(shutdown: broadcast::Sender<()>) -> Result<()> {
    let mut signals = [
        ("ALRM", signal(SignalKind::alarm())),
        ("HUP", signal(SignalKind::hangup())),
        ("INT", signal(SignalKind::interrupt())),
        ("QUIT", signal(SignalKind::quit())),
        ("TERM", signal(SignalKind::terminate())),
        ("USR1", signal(SignalKind::user_defined1())),
        ("USR2", signal(SignalKind::user_defined2())),
        ("PIPE", signal(SignalKind::pipe())),
    ]
    .into_iter()
    .try_fold(SelectAll::new(), |mut sig_stream, (sig, s)| {
        sig_stream.push(SignalStream::new(
            s.with_context(|| format!("failed to setup listener for SIG{sig}"))?,
        ));
        Ok::<_, ::anyhow::Error>(sig_stream)
    })
    .context("failed to setup signal listeners")?;

    let _h = ::tokio::spawn(async move {
        while signals.next().await.is_some() {
            warn!("Signal received; sending shutdown notification");
            if let Err(err) = shutdown.send(()) {
                error!(error = ?err, "Failed to send shutdown notification!");
                panic!("failed to send shutdown notification: {err:#}");
            }
        }
    });

    Ok(())
}

#[derive(Debug, ::serde::Deserialize)]
struct RequestSpecFile {
    url: String,
    #[serde(default)]
    headers: HashMap<String, String>,
}

#[derive(Debug, ::serde::Deserialize)]
struct RequestMapFile {
    #[serde(default)]
    format: Option<RequestSpecFile>,
    #[serde(default)]
    map: HashMap<String, RequestSpecFile>,
}

fn to_request_spec(file: RequestSpecFile) -> RequestSpec {
    RequestSpec {
        url: file.url,
        headers: file.headers,
    }
}

fn load_request_map(
    path: &Path,
) -> Result<(HashMap<CompactString, RequestSpec>, Option<RequestSpec>)> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read request map file '{}'", path.display()))?;
    let parsed: Result<RequestMapFile, _> = ::serde_json::from_str(&raw);
    if let Ok(map_file) = parsed {
        let map = map_file
            .map
            .into_iter()
            .map(|(k, v)| (CompactString::from(k), to_request_spec(v)))
            .collect();
        let format = map_file.format.map(to_request_spec);
        return Ok((map, format));
    }

    let map_only: HashMap<String, RequestSpecFile> =
        ::serde_json::from_str(&raw).context("failed to parse request map JSON")?;
    let map = map_only
        .into_iter()
        .map(|(k, v)| (CompactString::from(k), to_request_spec(v)))
        .collect();
    Ok((map, None))
}

#[::tokio::main]
async fn main() -> Result<()> {
    ::tracing_subscriber::fmt()
        .with_writer(io::stderr)
        .with_env_filter(EnvFilter::from_default_env())
        .with_span_events(FmtSpan::NEW | FmtSpan::CLOSE)
        .with_thread_ids(true)
        .with_line_number(true)
        .try_init()
        .map_err(|err| anyhow!("failed to initialize tracing subscriber: {err:#}"))?;

    let cli = ::argh::from_env::<Cli>();
    trace!("{cli:?}");

    let (shutdown, _) = broadcast::channel(1);
    setup_signals_handler(shutdown.clone())?;

    let (request_map, default_spec) = load_request_map(&cli.request_map)?;

    let (results_tx, results_rx) = mpsc::channel(1 << 15);
    let client = reqwest::Client::new();

    let sink_backend = HttpSink::new(results_rx);
    let sink_client =
        SinkClient::new(&cli.outfile, sink_backend).context("failed to create Sink client")?;
    let sink = ::tokio::spawn({
        let shutdown = shutdown.subscribe();
        async move { sink_client.run(shutdown).await }
    });

    let source_backend = HttpSource::new(client, request_map, default_spec, results_tx);
    let mut source_client = SourceClient::new(
        &cli.csv,
        None::<&str>,
        cli.seed,
        Poisson,
        cli.invoc_id,
        cli.minutes,
        source_backend,
        Duration::from_secs(cli.request_timeout_secs),
        &cli.minio_address,
        &cli.minio_bucket,
    )
    .context("failed to create Source client")?;

    let source = ::tokio::spawn({
        let shutdown = shutdown.subscribe();
        async move { source_client.run(shutdown).await }
    });

    match ::tokio::try_join!(source, sink) {
        Ok((source, sink)) => {
            match source {
                Ok(num_requests) => info!(?num_requests, "Source task joined"),
                Err(err) => error!(error = ?err, "Joined failed Source task: {err:#}"),
            }
            match sink {
                Ok(num_responses) => info!(?num_responses, "Sink task joined"),
                Err(err) => error!(error = ?err, "Joined failed Sink task: {err:#}"),
            }
        }
        Err(jerr) => {
            error!(error = ?jerr, "Failed to join Source and Sink tasks: {jerr:#}");
            return Err(jerr).context("Failed to join Source and Sink tasks");
        }
    }

    Ok(())
}
