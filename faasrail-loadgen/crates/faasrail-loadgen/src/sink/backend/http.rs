use std::convert::Infallible;

use tokio::sync::{broadcast, mpsc};
use tracing::{info, instrument, warn, Level};

use crate::http::HttpResponse;

#[derive(Debug)]
pub struct Http {
	rx: mpsc::Receiver<HttpResponse>,
}

impl Http {
	pub fn new(rx: mpsc::Receiver<HttpResponse>) -> Self {
		Self { rx }
	}
}

impl super::Backend for Http {
	type Error = Infallible;
	type Response = HttpResponse;

	#[instrument(level = Level::INFO, skip_all)]
	async fn run(
		mut self,
		to_appender: mpsc::Sender<Self::Response>,
		mut quit_rx: broadcast::Receiver<()>,
	) -> Result<u64, Self::Error> {
		let mut num_responses = 0;
		loop {
			::tokio::select! {
				received = quit_rx.recv() => {
					info!(?received, "Received shutdown notification");
					break;
				}
				resp = self.rx.recv() => {
					match resp {
						Some(resp) => {
							if to_appender.send(resp).await.is_err() {
								warn!("Appender channel closed; stopping HTTP sink");
								break;
							}
							num_responses += 1;
						}
						None => {
							info!("HTTP response channel closed; stopping HTTP sink");
							break;
						}
					}
				}
			}
		}

		drop(to_appender);
		Ok(num_responses)
	}
}
