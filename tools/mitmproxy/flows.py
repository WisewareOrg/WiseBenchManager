"""Print every flow in a mitmproxy flow file or HAR file as one JSON object per line.

Each line is `{"method": str | null, "url": str, "is_replay": str | null}`, in file order: `url` is
`request.url` for an HTTP flow and `tcp://<host>:<port>` from `server_conn.address` for any other,
whose `method` is null. Runs in mitmproxy's own environment with mitmproxy's own `FlowReader`, so
the runner reads flows without importing mitmproxy.

Usage: `python -I flows.py [--growing] <file>`. A read error exits non-zero with mitmproxy's
traceback on stderr. `--growing` marks a file mitmdump may still be writing: a read error at the
end of the snapshot is a partly written trailing record, and the flows before it are printed.
"""

import argparse
import io
import json
from pathlib import Path

from mitmproxy import exceptions, http
from mitmproxy.io import FlowReader


def _read(path, growing):
    """Read the flows in a snapshot of `path`.

    Args:
        path (str): The flow file or HAR file to read.
        growing (bool): Tolerate a read error at the end of the snapshot.

    Returns:
        list[mitmproxy.flow.Flow]: The flows, before any partly written trailing record.

    Raises:
        mitmproxy.exceptions.FlowReadException: The reader failed, and either `growing` is false
            or the failure is before the end of the snapshot.
    """
    data = Path(path).read_bytes()
    buf = io.BytesIO(data)
    flows = []
    try:
        for f in FlowReader(buf).stream():
            flows.append(f)
    except exceptions.FlowReadException:
        if not growing or buf.tell() < len(data):
            raise
    return flows


def _row(f):
    """Describe one flow by the fields the runner reads.

    Args:
        f (mitmproxy.flow.Flow): The flow.

    Returns:
        dict: `method`, `url` and `is_replay`, as the module docstring describes them.
    """
    if isinstance(f, http.HTTPFlow):
        return {"method": f.request.method, "url": f.request.url, "is_replay": f.is_replay}
    host, port = f.server_conn.address
    return {"method": None, "url": f"tcp://{host}:{port}", "is_replay": f.is_replay}


def main():
    """Print the flows of the file named on the command line, one JSON object per line."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--growing", action="store_true")
    parser.add_argument("path")
    args = parser.parse_args()
    for f in _read(args.path, args.growing):
        print(json.dumps(_row(f)))


if __name__ == "__main__":
    main()
