"""Entry point for doris-new-mcp server."""

import argparse
import os

from config.loader import AppConfig
from server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="doris-new-mcp: MCP Server for Apache Doris")
    parser.add_argument("--config-dir", default=os.environ.get("DORIS_MCP_CONFIG_DIR", "."))
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args()

    cfg = AppConfig(args.config_dir, env_file=args.env_file)

    mcp = create_server(config_dir=args.config_dir, env_file=args.env_file)

    from core.charset import CharsetMiddleware
    from core.request_logger import RequestLoggerMiddleware
    from starlette.middleware import Middleware
    mcp.run(
        transport="streamable-http",
        host=cfg.mcp.host,
        port=cfg.mcp.port,
        stateless_http=True,
        middleware=[Middleware(RequestLoggerMiddleware), Middleware(CharsetMiddleware)],
    )


if __name__ == "__main__":
    main()
