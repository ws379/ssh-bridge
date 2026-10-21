"""Repeat the real SSH file benchmark without sending file contents to stdout."""
import argparse
import json
from pathlib import Path
import statistics
import time
import server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=".bashrc")
    parser.add_argument("--runs", type=int, default=6)
    parser.add_argument("--max-bytes", type=int, default=16384)
    args = parser.parse_args()
    if not 2 <= args.runs <= 20:
        parser.error("runs must be in 2..20")
    times, sizes = [], []
    try:
        for _ in range(args.runs):
            started = time.perf_counter()
            result = server._read({"path": args.path, "max_bytes": args.max_bytes})
            times.append(round((time.perf_counter() - started) * 1000, 2))
            if result.get("isError"):
                raise RuntimeError(result["content"][0]["text"])
            sizes.append(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()))
        report = {"path": args.path, "max_bytes": args.max_bytes, "cold_ms": times[0],
                  "warm_median_ms": statistics.median(times[1:]), "all_ms": times,
                  "result_json_bytes": sizes[0], "catalog_tools": len(server.VISIBLE_TOOLS),
                  "catalog_json_bytes": len(json.dumps(server.VISIBLE_TOOLS))}
        print(json.dumps(report, indent=2))
    finally:
        for connection in server.RUNTIME.sessions.values():
            connection.close()


if __name__ == "__main__":
    main()
