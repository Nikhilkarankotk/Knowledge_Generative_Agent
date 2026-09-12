"""Run the FastAPI app or a one-off pipeline task from the command line."""

import argparse
import sys

from app.main import app

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Portfolio chatbot runner")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args, unknown = parser.parse_known_args()

    if unknown and unknown[0] == "check":
        import subprocess

        sys.exit(
            subprocess.call(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
            )
        )

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
