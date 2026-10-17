# FaaSRail Request Generator

```text
                '||''''|                .|'''|  '||'''|,              '||`
                 ||  .                  ||       ||   ||          ''   ||
                 ||''|   '''|.   '''|.  `|'''|,  ||...|'  '''|.   ||   ||
                 ||     .|''||  .|''||   .   ||  || \\   .|''||   ||   ||
                .||.    `|..||. `|..||.  |...|' .||  \\. `|..||. .||. .||.
```

Workload generator to use with FaaSRail shrinkray.

## Programs

### reqgen.py

Benchmark workload generator that sends HTTP requests to FaaS endpoints and measures performance.

**Usage:**
```bash
./reqgen.py -a <HOST:PORT> [OPTIONS] <BENCH>
```

**Arguments:**
- `-a, --address HOST:PORT` - Endpoint for benchmark's HTTP server (required)
- `BENCH` - Benchmark name (see available benchmarks below)

**Options:**
- `-v, --verbosity` - Increase logging verbosity (use `-v` or `-vv`)
- `--minio-address HOST:PORT` - MinIO server endpoint
- `--minio-bucket BUCKET_NAME` - MinIO bucket name
- `--ctr CONTAINER_ID` - Docker container ID for memory statistics
- `--out-stream` - Output JSON objects as newline-separated stream
- `--out-list` - Output as JSON list (default)

**Available Benchmarks:**
- `helloworld`
- `pyaes`
- `lr_serving` 
- `chameleon`
- `cnn_serving`
- `rnn_serving`
- `image_processing`
- `json_serdes`
- `matmul`
- `lr_training`
- `video_processing`

---

### post_process.py

Post-processes benchmark measurement files from a directory into a unified JSON structure.

**Usage:**
```bash
./post_process.py [OPTIONS] <DIR_PATH>
```

**Arguments:**
- `DIR_PATH` - Directory containing JSON measurement files

**Options:**
- `-v, --verbosity` - Increase logging verbosity (use `-v` or `-vv`)
- `-o, --output-file FILE_PATH` - Output file path (default: auto-generated in input directory)