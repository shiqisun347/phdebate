from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_transcript_collaboration_has_releasable_supervisor_runtime() -> None:
    build = (ROOT / "deploy/build-transcript-collab-release.sh").read_text()
    supervisor = (ROOT / "deploy/phdebate.supervisor.conf").read_text()
    package = (ROOT / "services/transcript-collab/package.json").read_text()

    assert '"$NPM" ci --ignore-scripts' in build
    assert '"$NPM" run check' in build
    assert ".transcript-collab-current" in supervisor
    assert "TRANSCRIPT_COLLAB_HMAC_SECRET" in supervisor
    assert "COLLAB_DATABASE_URL" in supervisor
    assert '"start": "node dist/src/index.js"' in package


def test_nginx_exposes_only_the_collaboration_websocket_proxy() -> None:
    standalone = (ROOT / "deploy/nginx-root.locations.conf").read_text()
    production = (ROOT / "deploy/jixia-nginx-root.conf").read_text()
    for config in (standalone, production):
        assert "location = /collab" in config
        assert "proxy_pass http://127.0.0.1:12400" in config
        assert "proxy_buffering off" in config
        assert "location ^~ /collab" not in config
