use std::{
	collections::HashMap,
	time::{Duration, Instant},
};

use compact_str::CompactString;
use tokio::sync::mpsc;
use tracing::{debug, error, instrument, warn, Level};

use crate::{http::HttpResponse, InvocationId, WorkloadRequest};

const STATUS_SEND_ERROR: u16 = 0;

#[derive(Debug, ::thiserror::Error)]
pub enum Error {
	#[error("missing URL mapping for bench '{bench}'")]
	MissingUrl { bench: CompactString },
}

#[derive(Debug, Clone)]
pub struct RequestSpec {
	pub url: String,
	pub headers: HashMap<String, String>,
}

#[derive(Debug, Clone)]
pub struct Http {
	client: reqwest::Client,
	request_map: HashMap<CompactString, RequestSpec>,
	default_spec: Option<RequestSpec>,
	results_tx: mpsc::Sender<HttpResponse>,
}

impl Http {
	pub fn new(
		client: reqwest::Client,
		request_map: HashMap<CompactString, RequestSpec>,
		default_spec: Option<RequestSpec>,
		results_tx: mpsc::Sender<HttpResponse>,
	) -> Self {
		Self {
			client,
			request_map,
			default_spec,
			results_tx,
		}
	}

	fn resolve_spec(&self, bench: &CompactString) -> Result<RequestSpec, Error> {
		self
			.request_map
			.get(bench)
			.cloned()
			.or_else(|| self.default_spec.clone())
			.ok_or_else(|| Error::MissingUrl { bench: bench.clone() })
	}

	fn apply_templates(&self, bench: &CompactString, spec: &RequestSpec) -> RequestSpec {
		let url = spec.url.replace("{bench}", bench.as_str());
		let headers = spec
			.headers
			.iter()
			.map(|(k, v)| (k.clone(), v.replace("{bench}", bench.as_str())))
			.collect();
		RequestSpec { url, headers }
	}
}

/// Milliseconds since the UNIX epoch, or `0` if the clock is before the epoch.
fn epoch_millis() -> u64 {
	match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
		Ok(dur) => dur.as_millis() as u64,
		Err(err) => {
			debug!(error = ?err, "SystemTime before UNIX_EPOCH; using 0 timestamp");
			0
		}
	}
}

impl super::Backend for Http {
	type Error = Error;

	#[instrument(level = Level::INFO, skip(self, wreq))]
	async fn issue(
		&self,
		invocation_id: InvocationId,
		wreq: &WorkloadRequest,
		minute: u16,
		timeout: Duration,
	) -> Result<(), Self::Error> {
		let spec = self.apply_templates(&wreq.bench, &self.resolve_spec(&wreq.bench)?);
		let bench = wreq.bench.clone();

		// The server-side handler expects `payload` to be a list of byte values.
		let payload_bytes: Vec<u8> = wreq.payload.as_bytes().to_vec();
		let body = serde_json::json!({ "payload": payload_bytes }).to_string();

		let client = self.client.clone();
		let results_tx = self.results_tx.clone();

		// True open loop: hand the waiting off to a detached task and return immediately, 
		::tokio::spawn(async move {
			let mut request = client.post(&spec.url).body(body);
			for (name, value) in &spec.headers {
				request = request.header(name, value);
			}

			// Measure end-to-end latency from the moment the request is actually sent.
			let start = Instant::now();
			let status_code = match ::tokio::time::timeout(timeout, request.send()).await {
				Ok(Ok(resp)) => resp.status().as_u16(),
				Ok(Err(err)) => {
					warn!(error = ?err, url = %spec.url, %invocation_id, minute, "HTTP request failed");
					STATUS_SEND_ERROR
				}
				Err(_) => {
					warn!(url = %spec.url, %invocation_id, minute, ?timeout, "HTTP request timed out");
					STATUS_SEND_ERROR
				}
			};

			let response = HttpResponse {
				invocation_id,
				bench,
				status_code,
				latency_ms: start.elapsed().as_millis() as u64,
				timestamp_ms: epoch_millis(),
			};

			if let Err(err) = results_tx.send_timeout(response, timeout).await {
				error!(error = ?err, minute, "Failed to send HTTP response to Sink");
			}
		});

		Ok(())
	}
}
