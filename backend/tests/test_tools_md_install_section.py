from app.services.tools_md_builder import generate_tools_md


def test_install_request_section_included_when_agents_manage_scope():
    tools_md = generate_tools_md(
        name="Boss", emoji="🎯", raw_token="tok_abc", board_id="b1",
        is_board_lead=True, scopes=["agents:manage", "tasks:read"],
    )
    assert "Installation Requests" in tools_md
    assert "POST /api/v1/agent/install-requests" in tools_md
    assert "uninstall" in tools_md.lower()


def test_install_request_section_omitted_without_scope():
    tools_md = generate_tools_md(
        name="Cody", emoji="🤖", raw_token="tok_xyz", board_id="b1",
        is_board_lead=False, scopes=["tasks:read", "tasks:write"],
    )
    assert "Installation Requests" not in tools_md
    assert "install-requests" not in tools_md
