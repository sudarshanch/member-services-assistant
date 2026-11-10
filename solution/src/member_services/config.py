"""Runtime configuration. The pack location and the model provider are both set here."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Read solution/.env into the environment. Values in that file replace placeholders."""
    if os.environ.get("MEMBER_SERVICES_SKIP_DOTENV") == "1":
        return
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, value = stripped.split("=", 1)
        name = key.strip()
        os.environ[name] = value.strip().strip('"').strip("'")


def discover_pack_dir() -> Path:
    env = os.environ.get("PACK_DIR")
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "mock_services" / "server.py").exists() and (candidate / "kb").is_dir():
            return candidate
    raise SystemExit(
        "Set PACK_DIR to the task pack root (the directory that contains kb/ and mock_services/)."
    )


@dataclass
class TenantConfig:
    tenant_id: str
    name: str
    token: str
    networks: list[str]


@dataclass
class Settings:
    pack_dir: Path
    mock_base_url: str
    provider: str
    model_name: str
    openai_base_url: str
    openai_api_key: str
    max_steps: int
    max_cost_usd: float
    max_turn_latency_s: float
    tenants: dict[str, TenantConfig]
    directory_api_key: str
    secrets: list[str] = field(default_factory=list)

    @property
    def kb_dir(self) -> Path:
        return self.pack_dir / "kb"

    @property
    def eval_path(self) -> Path:
        return self.pack_dir / "eval" / "conversations.json"

    @property
    def manifest_path(self) -> Path:
        return self.pack_dir / "tools" / "third_party_provider_directory.json"


def load_settings(pack_dir: Path | None = None) -> Settings:
    _load_dotenv()
    pack = Path(pack_dir).resolve() if pack_dir else discover_pack_dir()
    tenants_path = pack / "mock_services" / "data" / "tenants.json"
    if not tenants_path.exists():
        raise SystemExit(f"Tenant config not found at {tenants_path}. Check PACK_DIR.")
    raw = json.loads(tenants_path.read_text(encoding="utf-8"))
    tenants: dict[str, TenantConfig] = {}
    for tenant_id, info in raw["tenants"].items():
        token = os.environ.get(f"{tenant_id.upper()}_TOKEN", info["api_token"])
        networks = [product["network_id"] for product in info.get("products", [])]
        tenants[tenant_id] = TenantConfig(
            tenant_id=tenant_id,
            name=info["name"],
            token=token,
            networks=networks,
        )
    directory_key = os.environ.get("DIRECTORY_API_KEY", raw["directory"]["api_key"])
    secrets = [tenant.token for tenant in tenants.values()] + [directory_key]
    return Settings(
        pack_dir=pack,
        mock_base_url=os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:8090").rstrip("/"),
        provider=os.environ.get("MODEL_PROVIDER", "deterministic").strip().lower(),
        model_name=os.environ.get("MODEL_NAME", "deterministic-policy-v1"),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        max_steps=int(os.environ.get("MAX_STEPS", "12")),
        max_cost_usd=float(os.environ.get("MAX_COST_USD", "0.03")),
        max_turn_latency_s=float(os.environ.get("MAX_TURN_LATENCY_S", "45")),
        tenants=tenants,
        directory_api_key=directory_key,
        secrets=[item for item in secrets if item],
    )
