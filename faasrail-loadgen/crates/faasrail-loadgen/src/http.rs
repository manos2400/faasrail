use serde::{Deserialize, Serialize};

use crate::InvocationId;

/// HTTP invocation response metadata produced by the load generator.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HttpResponse {
    pub invocation_id: InvocationId,
    pub bench: ::compact_str::CompactString,
    pub status_code: u16,
    pub latency_ms: u64,
    pub timestamp_ms: u64,
}
