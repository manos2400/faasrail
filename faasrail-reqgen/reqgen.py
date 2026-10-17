#!/usr/bin/env python3
#
# mkolydakis, Tue Jan 06 11:53:00 AM EET 2026

import argparse
from datetime import datetime
from itertools import combinations
import json
import logging
import os
import shlex
import statistics
import sys
import time
from typing import Generator, Iterable

import subprocess
import requests


VERSION = "0.0.4"

DEFAULT_MINIO_ADDRESS = os.getenv("MINIO_ADDRESS") or "icy1.cslab.ece.ntua.gr:59000"
DEFAULT_MINIO_BUCKET_NAME = os.getenv("MINIO_BUCKET_NAME") or "snaplace-fbpml"

COLD_RUNS = 2
WARM_RUNS = 10

DOCKER_EXEC_CMD = "docker exec {ctr} sh -c {script}"


def payloadize(d):
    return list(json.dumps(d).encode())


class Issuer(object):
    """Issue an HTTP request to function endpoint.

    Sends POST to `http://{HOST:PORT}/invoke` with the given JSON request body.

    Measures client-side time and returns a dict containing the server JSON
    response plus a `client_ms` field.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.session = requests.Session()

    def __call__(self, json_func_req: str) -> dict:
        url = f"http://{self.args.address}/invoke"
        start = time.perf_counter()
        r = self.session.post(
            url,
            data=json_func_req,
            headers={"Content-Type": "application/json"},
            timeout=90,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        r.raise_for_status()
        try:
            payload = r.json()
        except requests.JSONDecodeError:
            logging.error("HTTP response is not JSON: %s", r.text)
            raise

        if isinstance(payload, dict):
            payload = dict(payload)
            payload["client_ms"] = elapsed_ms
            return payload

        # If server returns non-object JSON, still keep measurement format stable.
        return {"response": payload, "client_ms": elapsed_ms}


class Bench(object):
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.issuer = Issuer(args)
        self.outfile = f"{args.bench}__{datetime.today().strftime('%Y%m%d%H%M%S')}.json"

    @staticmethod
    def generate_request() -> str:
        raise NotImplementedError("Method should be implemented in subclasses")

    def generate_all_requests(self) -> Generator[str, None, None]:
        raise NotImplementedError("Method should be implemented in subclasses")

    def measure_all(self):
        for req in self.generate_all_requests():
            measurements = [
                self.issuer(req)["client_ms"] for _ in range(COLD_RUNS + WARM_RUNS)
            ]
            mean = statistics.fmean(measurements[COLD_RUNS:])
            stdev = statistics.stdev(measurements[COLD_RUNS:])
            mem = self.peek_ctr_mem() if self.args.ctr else None
            logging.debug(
                f"mean: {mean}, stdev: {stdev}, "
                f"measurements: {measurements}, mem: {mem}"
            )
            yield {
                req: {
                    "mean": mean,
                    "stdev": stdev,
                    "all": measurements,
                    "mem": mem,
                }
            }

    def peek_ctr_mem(self) -> dict[str, float]:
        """Capture virtual memory statistics of benchmark's server process.

        Assumes the server runs under `gunicorn` in the container.
        """
        script = (
            "pid=;"
            " for f in /proc/[0-9]*/status; do"
            "   case \"$(grep '^Name:' \"$f\" 2>/dev/null)\" in"
            "     *gunicorn*|*python*)"
            "       pid=\"${f#/proc/}\"; pid=\"${pid%/status}\";;"
            "   esac;"
            " done;"
            " [ -n \"$pid\" ] && grep '^Vm' /proc/$pid/status"
        )

        cmd = shlex.split(DOCKER_EXEC_CMD.format(ctr=self.args.ctr, script=shlex.quote(script)))
        p = subprocess.run(cmd, capture_output=True, timeout=20)
        if p.returncode != 0:
            logging.error(
                "peek_ctr_mem failed (rc=%d): %s",
                p.returncode,
                (p.stderr.decode(errors="replace") or p.stdout.decode(errors="replace")).strip(),
            )
            return {}

        ret = {}
        for line in p.stdout.decode().strip().split("\n"):
            parts = line.split()
            if len(parts) < 2:
                continue
            vm_stat_name = parts[0].rstrip(":")
            try:
                vm_stat_value_mib = int(parts[1]) * 1000 / 1024.0 / 1024.0
            except ValueError:
                continue
            ret[vm_stat_name] = vm_stat_value_mib
        return ret

    def export_all_stream(self):
        with open(self.outfile, "w") as fout:
            for out in self.measure_all():
                json.dump(out, fout)
                print(file=fout)

    def export_all_list(self):
        out = list(self.measure_all())
        with open(self.outfile, "w") as fout:
            json.dump(out, fout, indent=4)


class Helloworld(Bench):
    INPUTS = None

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request() -> str:
        return json.dumps(
            {
                "payload": payloadize({"input": None}),
                "metadata_map": {"header-empty": ""},
            }
        )

    def generate_all_requests(self):
        req = self.generate_request()
        logging.debug(req)
        yield req


class Pyaes(Bench):
    INPUTS = [
        (range(16, 1023, 16), range(1, 250 + 2, 25)),
        (range(1024, 8191, 64), range(1, 125 + 2, 25)),
        (range(8192, 32768 + 1, 512), range(1, 50 + 2, 10)),
    ]

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(message_length: int, num_iterations: int) -> str:
        return json.dumps(
            {
                "payload": payloadize(
                    {
                        "message_length": message_length,
                        "num_iterations": num_iterations,
                    }
                ),
                "metadata_map": {
                    "header-message-length": str(message_length),
                    "header-num-iterations": str(num_iterations),
                },
            }
        )

    def generate_all_requests(self):
        for msg_len_range, num_iters_range in self.inputs:
            for msg_len in msg_len_range:
                for num_iters in num_iters_range:
                    req = self.generate_request(msg_len, num_iters)
                    logging.debug(req)
                    yield req


class LrServing(Bench):
    INPUTS = {
        "size": [10, 20, 50, 100],
        "x": [
            "The ambiance is magical. The food and service was nice! The lobster and cheese was to die for and our steaks were cooked perfectly.  ",
            "My favorite cafe. I like going there on weekends, always taking a cafe and some of their pastry before visiting my parents.  ",
        ],
    }

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(
        minio_address: str,
        bucket_name: str,
        model_object_key: str,
        tfidf_vect_object_key: str,
        x: str,
    ) -> str:
        return json.dumps(
            {
                "payload": payloadize(
                    {
                        "minio_address": minio_address,
                        "bucket_name": bucket_name,
                        "model_object_key": model_object_key,
                        "tfidf_vect_object_key": tfidf_vect_object_key,
                        "x": x,
                    }
                ),
                "metadata_map": {
                    "header-minio-address": minio_address,
                    "header-bucket-name": bucket_name,
                    "header-model-object-key": model_object_key,
                    "header-tfidf-vect-object-key": tfidf_vect_object_key,
                    "header-x": x,
                },
            }
        )

    def generate_all_requests(self):
        for size in self.inputs["size"]:
            model_object_key = f"lr_model_reviews{size}mb.pk"
            tfidf_vect_object_key = f"lr_vectorizer_reviews{size}mb.pk"
            for x in self.inputs["x"]:
                req = self.generate_request(
                    minio_address=self.args.minio_address,
                    bucket_name=self.args.minio_bucket,
                    model_object_key=model_object_key,
                    tfidf_vect_object_key=tfidf_vect_object_key,
                    x=x,
                )
                logging.debug(req)
                yield req


class Chameleon(Bench):
    INPUTS = [
        range(4, 127, 4),
        range(128, 767, 8),
        range(768, 1023, 16),
        range(1024, 1536 + 1, 32),
    ]

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(nrow: int, ncol: int) -> str:
        return json.dumps(
            {
                "payload": payloadize({"nrow": nrow, "ncol": ncol}),
                "metadata_map": {
                    "header-nrow": str(nrow),
                    "header-ncol": str(ncol),
                },
            }
        )

    def generate_all_requests(self):
        for rng in self.inputs:
            for s in rng:
                req = self.generate_request(s, s)
                logging.debug(req)
                yield req


class CnnServing(Bench):
    INPUTS = [0, 1]

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(img_idx: int) -> str:
        return json.dumps(
            {
                "payload": payloadize({"img_idx": img_idx}),
                "metadata_map": {"header-img-idx": str(img_idx)},
            }
        )

    def generate_all_requests(self):
        for img_idx in self.inputs:
            req = self.generate_request(img_idx)
            logging.debug(req)
            yield req


class RnnServing(Bench):
    INPUTS = [
        ("Scottish", "ABCDEFGHIJKLMNOP", "rnn_params.pkl", "rnn_model.pth"),
        ("Greek", "QRSTUVWXYZABCDEF", "rnn_params.pkl", "rnn_model.pth"),
        ("Vietnamese", "ABCXYZ", "rnn_params.pkl", "rnn_model.pth"),
        (
            "Chinese",
            "BCDEFGHIJKLMNOPQRSTUVWXY",
            "rnn_params.pkl",
            "rnn_model.pth",
        ),
    ]

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(
        minio_address: str,
        bucket_name: str,
        language: str,
        start_letters: str,
        model_parameter_obj_key: str,
        model_object_key: str,
    ) -> str:
        return json.dumps(
            {
                "payload": payloadize(
                    {
                        "minio_address": minio_address,
                        "bucket_name": bucket_name,
                        "language": language,
                        "start_letters": start_letters,
                        "model_parameter_object_key": model_parameter_obj_key,
                        "model_object_key": model_object_key,
                    }
                ),
                "metadata_map": {
                    "header-minio-address": minio_address,
                    "header-bucket-name": bucket_name,
                    "header-language": language,
                    "header-start-letters": start_letters,
                    "header-model-param-obj-key": model_parameter_obj_key,
                    "header-model-object-key": model_object_key,
                },
            }
        )

    def generate_all_requests(self) -> Iterable[str]:
        for t in self.inputs:
            req = self.generate_request(
                minio_address=self.args.minio_address,
                bucket_name=self.args.minio_bucket,
                language=t[0],
                start_letters=t[1],
                model_parameter_obj_key=t[2],
                model_object_key=t[3],
            )
            logging.debug(req)
            yield req


class ImageProcessing(Bench):
    INPUTS = {
        "size": (f"{size:03}" for size in [16, 20, 32, 72, 99, 107, 146, 201, 230]),
        "ops": ["filter", "flip", "gray_scale", "resize", "rotate"],
    }

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(
        minio_address: str, bucket_name: str, img_name: str, ops: list[str]
    ) -> str:
        ops_joined = "-".join(ops)
        return json.dumps(
            {
                "payload": payloadize(
                    {
                        "minio_address": minio_address,
                        "bucket_name": bucket_name,
                        "img_name": img_name,
                        "ops": ops_joined,
                    }
                ),
                "metadata_map": {
                    "header-minio-address": minio_address,
                    "header-bucket-name": bucket_name,
                    "header-img-name": img_name,
                    "header-ops": ops_joined,
                },
            }
        )

    def generate_all_requests(self):
        for size in self.inputs["size"]:
            for by in range(1, len(self.inputs["ops"]) + 1):
                for c in combinations(self.inputs["ops"], by):
                    req = self.generate_request(
                        minio_address=self.args.minio_address,
                        bucket_name=self.args.minio_bucket,
                        img_name=f"img{size}k.jpeg",
                        ops=list(c),
                    )
                    logging.debug(req)
                    yield req


class JsonSerdes(Bench):
    INPUT = (
        f"{n}.json"
        for n in [
            1,
            2,
            "search",
            "gen02_n1024",
            "gen02_n1408",
            "gen02_n1792",
            "gen02_n2176",
            "gen02_n2560",
            "gen02_n2944",
            "gen02_n3328",
            "gen02_n3712",
            "gen02_n4096",
            "gen02_n4480",
            "gen02_n4864",
            "gen02_n5248",
            "gen02_n5632",
            "gen02_n6016",
            "gen02_n6400",
            "gen02_n6784",
            "gen02_n7168",
            "gen02_n7936",
            "gen02_n8704",
            "gen02_n9472",
            "gen02_n10240",
            "gen02_n11008",
            "gen02_n11776",
            "gen02_n12544",
            "gen02_n13312",
            "gen02_n14848",
            "gen02_n16384",
            "gen02_n17920",
            "gen02_n19456",
        ]
    )

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUT

    @staticmethod
    def generate_request(minio_address: str, bucket_name: str, file_name: str) -> str:
        return json.dumps(
            {
                "payload": payloadize(
                    {
                        "minio_address": minio_address,
                        "bucket_name": bucket_name,
                        "file_name": file_name,
                    }
                ),
                "metadata_map": {
                    "header-minio-address": minio_address,
                    "header-bucket-name": bucket_name,
                    "header-file-name": file_name,
                },
            }
        )

    def generate_all_requests(self):
        for file_name in self.inputs:
            req = self.generate_request(
                minio_address=self.args.minio_address,
                bucket_name=self.args.minio_bucket,
                file_name=file_name,
            )
            logging.debug(req)
            yield req


class Matmul(Bench):
    INPUTS = [
        range(8, 127, 4),
        range(128, 767, 8),
        range(768, 1023, 16),
        range(1024, 1536 + 1, 32),
    ]

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(N: int, M: int) -> str:
        return json.dumps(
            {
                "payload": payloadize({"N": N, "M": M}),
                "metadata_map": {"header-N": str(N), "header-M": str(M)},
            }
        )

    def generate_all_requests(self):
        for rnge in self.inputs:
            for n in rnge:
                req = self.generate_request(n, n)
                logging.debug(req)
                yield req


class LrTraining(Bench):
    INPUTS = {"size": [10, 20, 50]}

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(minio_address: str, bucket_name: str, dataset_object_key: str) -> str:
        return json.dumps(
            {
                "payload": payloadize(
                    {
                        "minio_address": minio_address,
                        "bucket_name": bucket_name,
                        "dataset_object_key": dataset_object_key,
                    }
                ),
                "metadata_map": {
                    "header-minio-address": minio_address,
                    "header-bucket-name": bucket_name,
                    "header-dataset-object-key": dataset_object_key,
                },
            }
        )

    def generate_all_requests(self):
        for size in self.inputs["size"]:
            dataset_object_key = f"reviews{size}mb.csv"
            req = self.generate_request(
                minio_address=self.args.minio_address,
                bucket_name=self.args.minio_bucket,
                dataset_object_key=dataset_object_key,
            )
            logging.debug(req)
            yield req


class VideoProcessing(Bench):
    INPUTS = [
        "big_buck_bunny_240p_1mb.mp4",
        "big_buck_bunny_360p_1mb.mp4",
        "big_buck_bunny_480p_1mb.mp4",
        "big_buck_bunny_720p_1mb.mp4",
        "big_buck_bunny_240p_2mb.mp4",
        "big_buck_bunny_360p_2mb.mp4",
        "big_buck_bunny_480p_2mb.mp4",
        "big_buck_bunny_240p_5mb.mp4",
        "big_buck_bunny_240p_10mb.mp4",
    ]

    def __init__(self, args: argparse.Namespace, inputs=None) -> None:
        super().__init__(args)
        self.inputs = inputs or self.INPUTS

    @staticmethod
    def generate_request(minio_address: str, bucket_name: str, vid_name: str) -> str:
        return json.dumps(
            {
                "payload": payloadize(
                    {
                        "minio_address": minio_address,
                        "bucket_name": bucket_name,
                        "vid_name": vid_name,
                    }
                ),
                "metadata_map": {
                    "header-minio-address": minio_address,
                    "header-bucket-name": bucket_name,
                    "header-vid-name": vid_name,
                },
            }
        )

    def generate_all_requests(self):
        for vid_name in self.inputs:
            req = self.generate_request(
                minio_address=self.args.minio_address,
                bucket_name=self.args.minio_bucket,
                vid_name=vid_name,
            )
            logging.debug(req)
            yield req


BENCH_CLS = {
    "helloworld": Helloworld,
    "pyaes": Pyaes,
    "lr_serving": LrServing,
    "chameleon": Chameleon,
    "cnn_serving": CnnServing,
    "rnn_serving": RnnServing,
    "image_processing": ImageProcessing,
    "json_serdes": JsonSerdes,
    "matmul": Matmul,
    "lr_training": LrTraining,
    "video_processing": VideoProcessing,
}


def parse_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Workload generator for FaaSRail shrinkray - mkolydakis"),
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s v{VERSION}"
    )
    parser.add_argument(
        "-v",
        "--verbosity",
        action="count",
        dest="verbosity",
        default=0,
        help="set logging verbosity (maxes out at '-vv')",
    )
    parser.add_argument(
        "-a",
        "--address",
        metavar="HOST:PORT",
        required=True,
        help="endpoint for benchmark's HTTP server (HOST:PORT)",
    )
    parser.add_argument(
        "--minio-address",
        metavar="HOST:PORT",
        default=DEFAULT_MINIO_ADDRESS,
        help=f"endpoint of MinIO server [default: '{DEFAULT_MINIO_ADDRESS}']",
    )
    parser.add_argument(
        "--minio-bucket",
        metavar="BUCKET_NAME",
        default=DEFAULT_MINIO_BUCKET_NAME,
        help=f"name of the MinIO bucket [default: '{DEFAULT_MINIO_BUCKET_NAME}']",
    )
    parser.add_argument(
        "--ctr",
        metavar="CONTAINER_ID",
        default=None,
        help="Docker's CONTAINER ID to exec into benchmark server for mem stats",
    )
    parser.add_argument(
        "bench",
        metavar="BENCH",
        choices=BENCH_CLS.keys(),
        help="benchmark name (e.g., 'image_processing')",
    )
    json_output_parser = parser.add_mutually_exclusive_group(required=False)
    json_output_parser.add_argument(
        "--out-stream",
        dest="json_stream",
        action="store_true",
        help="output a stream of JSON objects ('\\n'-separated)",
    )
    json_output_parser.add_argument(
        "--out-list",
        dest="json_stream",
        action="store_false",
        help="output a JSON list (default)",
    )
    return parser.parse_args(args=args)


def main(argv: list[str]) -> int:
    lfmt = "%(asctime)s %(name)-8s %(module)s %(levelname)-8s    %(message)s"
    llvl = logging.WARNING
    try:
        args = parse_args(argv[1:])

        if args.verbosity == 1:
            llvl = logging.INFO
        elif args.verbosity >= 2:
            llvl = logging.DEBUG
        logging.basicConfig(format=lfmt, level=llvl)

        bench = BENCH_CLS[args.bench](args)
        logging.debug(f"output in: {bench.outfile}")

        if args.json_stream:
            bench.export_all_stream()
        else:
            bench.export_all_list()
    except Exception as e:
        logging.exception(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
