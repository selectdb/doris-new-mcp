"""Entry point for doris-new-mcp server."""

import argparse
import os

from config.loader import AppConfig
from server import _decode_webui_session_cookie, create_server, get_machine_ip


def main() -> None:
    parser = argparse.ArgumentParser(description="doris-new-mcp: MCP Server for Apache Doris")
    parser.add_argument("--config-dir", default=os.environ.get("DORIS_MCP_CONFIG_DIR", "."))
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args()

    cfg = AppConfig(args.config_dir, env_file=args.env_file)
    machine_ip = get_machine_ip()

    mcp = create_server(
        config_dir=args.config_dir,
        env_file=args.env_file,
        machine_ip=machine_ip,
    )

    from core.charset import CharsetMiddleware
    from core.session_affinity_proxy import SessionAffinityProxyMiddleware
    from core.request_logger import RequestLoggerMiddleware
    from starlette.middleware import Middleware
    mcp.run(
        transport="streamable-http",
        host=cfg.mcp.host,
        port=cfg.mcp.port,
        stateless_http=True,
        middleware=[
            Middleware(RequestLoggerMiddleware),
            Middleware(
                SessionAffinityProxyMiddleware,
                decoder=_decode_webui_session_cookie,
                local_ip=machine_ip,
                target_port=cfg.mcp.port,
            ),
            Middleware(CharsetMiddleware),
        ],
    )


if __name__ == "__main__":
    main()
