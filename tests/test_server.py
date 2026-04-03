from alpha_agents.tools.registry import create_tools_server


def test_create_server_returns_server_config():
    server = create_tools_server()
    assert server is not None
