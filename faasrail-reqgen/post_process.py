#!/usr/bin/env python3
#
# mkolydakis, Tue Jan 06 11:53:00 AM EET 2026

import argparse
from datetime import datetime
import json
import logging
import math
import os
import sys


__version__ = "0.0.4"


_DEFAULT_OUTFILE = "post_process.json"

_POOR_BENCHES = [
    # "helloworld",
    "lr_serving",
    "rnn_serving",
    "cnn_serving",
    "lr_training",
    "video_processing",
]
_RICH_BENCHES = [
    "pyaes",
    "chameleon",
    "matmul",
    "json_serdes",
    "image_processing",
]
_VALID_BENCH_NAMES = _POOR_BENCHES + _RICH_BENCHES


def extract_payload(raw_input: str):
    return bytes(json.loads(raw_input)["payload"]).decode()


def process_file(file_path: str):
    return process_file_list(file_path)


def process_dir(dir_path: str):
    return process_dir_list(dir_path)


def export_json(args: argparse.Namespace, out):
    return export_json_list(args, out)


#######################################################################################
#
# output DICT
#
#######################################################################################


def process_file_dict(file_path: str):
    """Output *dict*"""
    with open(file_path, "r") as fin:
        data = json.load(fin)
    ret = {}
    for measurement in data:
        logging.debug(f"measurement = {measurement}")
        assert len(measurement.keys()) == 1 and len(measurement.values()) == 1

        bench_name = os.path.basename(file_path).split("__")[0]
        bench_input = list(measurement.keys())[0]
        tmp = list(measurement.values())[0]
        mean = tmp["mean"]
        stdev = tmp["stdev"]
        ret[mean] = {
            "stdev": stdev,
            "bench": bench_name,
            # "raw_input": bench_input,
            "payload": extract_payload(bench_input),
        }
    return ret


def process_dir_dict(dir_path: str):
    """Output *dict*"""
    ret = {}
    for direntry in os.scandir(dir_path):
        bench = direntry.name.split("__")[0]
        if not direntry.is_file() or bench not in _VALID_BENCH_NAMES:
            logging.debug(f"Ignoring {direntry.path}")
            continue
        logging.debug(
            f"Processing benchmark '{bench}' (file '{direntry.path}')"
        )

        for k, v in process_file_dict(direntry.path).items():
            if k in ret:
                logging.warning(
                    f"Overwriting entry for key '{k}':  '{ret[k]}'  becomes  '{v}'"
                )
            ret[k] = v
    return ret


def export_json_dict(args: argparse.Namespace, out):
    """Export sorted JSON *object*"""
    with open(args.output_file, "w") as fout:
        json.dump(out, fout, sort_keys=True)

    bn, ext = args.output_file.rsplit(".", 1)
    outfile_basename = bn if ext == "json" else args.output_file
    with open(outfile_basename + "__poor.json", "w") as fout:
        json.dump(
            dict(
                filter(
                    lambda meas: meas[1]["bench"] in _POOR_BENCHES, out.items()
                )
            ),
            fout,
            sort_keys=True,
        )
    with open(outfile_basename + "__rich.json", "w") as fout:
        json.dump(
            dict(
                filter(
                    lambda meas: meas[1]["bench"] in _RICH_BENCHES, out.items()
                )
            ),
            fout,
            sort_keys=True,
        )


#######################################################################################
#
# output LIST
#
#######################################################################################


def process_file_list(file_path: str):
    """Output *sorted list*"""
    with open(file_path, "r") as fin:
        data = json.load(fin)
    ret = []
    for measurement in data:
        logging.debug(f"measurement = {measurement}")
        assert len(measurement.keys()) == 1 and len(measurement.values()) == 1

        bench_name = os.path.basename(file_path).split("__")[0]
        bench_input = list(measurement.keys())[0]
        tmp = list(measurement.values())[0]
        mean = tmp["mean"]
        stdev = tmp["stdev"]
        ret.append(
            {
                "mean": mean,
                "stdev": stdev,
                "mem_mib": math.ceil(tmp["mem"]["VmHWM"]),
                "bench": bench_name,
                # "raw_input": bench_input,
                "payload": extract_payload(bench_input),
            }
        )
    return sorted(ret, key=lambda e: e["mean"])


def process_dir_list(dir_path: str):
    """Output *sorted list*"""
    ret = []
    for direntry in os.scandir(dir_path):
        bench = direntry.name.split("__")[0]
        if not direntry.is_file() or bench not in _VALID_BENCH_NAMES:
            logging.info(f"Ignoring {direntry.path}")
            continue
        logging.debug(
            f"Processing benchmark '{bench}' (file '{direntry.path}')"
        )

        ret.extend(process_file_list(direntry.path))
    return sorted(ret, key=lambda e: e["mean"])


def export_json_list(args: argparse.Namespace, out):
    """Export sorted JSON *list*"""
    with open(args.output_file, "w") as fout:
        json.dump(out, fout)

    bn, ext = args.output_file.rsplit(".", 1)
    outfile_basename = bn if ext == "json" else args.output_file
    with open(outfile_basename + "__poor.json", "w") as fout:
        json.dump(
            list(filter(lambda meas: meas["bench"] in _POOR_BENCHES, out)),
            fout,
        )
    with open(outfile_basename + "__rich.json", "w") as fout:
        json.dump(
            list(filter(lambda meas: meas["bench"] in _RICH_BENCHES, out)),
            fout,
        )


#######################################################################################


def parse_args(argv) -> argparse.Namespace:
    """Parse the command line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Process workloads' measurements into a single structure indexed "
            "by mean time -- ckatsak"
        )
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s v{__version__}"
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
        "input_dir",
        metavar="DIR_PATH",
        help="local directory containing the JSON files with measurement results",
    )
    parser.add_argument(
        "-o",
        "--output-file",
        metavar="FILE_PATH",
        default=_DEFAULT_OUTFILE,
        help="the output JSON file [default: inside input DIR_PATH and named after it]",
    )
    args = parser.parse_args(args=argv)
    if args.output_file == _DEFAULT_OUTFILE:
        args.output_file = f"{args.input_dir}__{datetime.today().strftime('%Y%m%d%H%M%S')}.json"
        args.output_file = os.path.join(args.input_dir, args.output_file)
    return args


def main(argv):
    lfmt = "%(asctime)s %(name)-8s %(module)s %(levelname)-8s    %(message)s"
    llvl = logging.WARNING
    try:
        args = parse_args(argv)

        # Setup logging verbosity
        if args.verbosity == 1:
            llvl = logging.INFO
        elif args.verbosity >= 2:
            llvl = logging.DEBUG
        logging.basicConfig(format=lfmt, level=llvl)

        out = process_dir(args.input_dir)
        export_json(args, out)
    except Exception as e:
        logging.exception(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
