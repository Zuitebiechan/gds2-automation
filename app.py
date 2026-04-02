"""Backward-compatible project entrypoint for the HTTP server."""

from server.app import app, create_app, main


if __name__ == "__main__":
    raise SystemExit(main())
