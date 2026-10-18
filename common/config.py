# ============================================================
# COMPONENT: C0 - Common Configuration Loader
# PURPOSE: Load and validate environment settings from .env into a typed Settings object.
# INPUT: .env file or system environment variables.
# OUTPUT: Typed Settings instance and resolved data directory Path.
# CALLED BY: All pipeline components (C1-C7)      CALLS: common/trace.py, python-dotenv, pydantic
# IF IT BREAKS, CHECK: Missing required variables in .env, invalid DATA_DIR path permissions.
# ============================================================

import os
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from common.trace import ComponentError, trace

REQUIRED_VARS = [
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "EVOLUS_BASE_URL",
    "EVOLUS_API_KEY",
    "EVOLUS_APP_ID",
    "DATA_DIR",
]


class Settings(BaseModel):
    """Typed application settings model loaded from environment."""
    model_config = ConfigDict(extra="ignore")

    llm_base_url: str = Field(description="Base URL for OpenAI-compatible LLM endpoint")
    llm_api_key: str = Field(description="Authentication token or API key for LLM endpoint")
    llm_model: str = Field(description="Target model identifier (e.g., google/gemma-4-26B-A4B-it)")
    evolus_base_url: str = Field(description="Base URL for Evolus REST services")
    evolus_api_key: str = Field(description="API key for Evolus REST services")
    evolus_app_id: str = Field(description="Workspace or App ID for Evolus REST services")
    data_dir: Path = Field(description="Local root directory for data, packs, runs, and artifacts")

    @property
    def LLM_BASE_URL(self) -> str:
        """IN: None (property reader).
        OUT: LLM base URL string.
        FAILS WITH: Never fails."""
        return self.llm_base_url

    @property
    def LLM_API_KEY(self) -> str:
        """IN: None (property reader).
        OUT: LLM API key string.
        FAILS WITH: Never fails."""
        return self.llm_api_key

    @property
    def LLM_MODEL(self) -> str:
        """IN: None (property reader).
        OUT: Target model identifier string.
        FAILS WITH: Never fails."""
        return self.llm_model

    @property
    def EVOLUS_BASE_URL(self) -> str:
        """IN: None (property reader).
        OUT: Evolus service base URL string.
        FAILS WITH: Never fails."""
        return self.evolus_base_url

    @property
    def EVOLUS_API_KEY(self) -> str:
        """IN: None (property reader).
        OUT: Evolus service API key string.
        FAILS WITH: Never fails."""
        return self.evolus_api_key

    @property
    def EVOLUS_APP_ID(self) -> str:
        """IN: None (property reader).
        OUT: Evolus application/workspace ID string.
        FAILS WITH: Never fails."""
        return self.evolus_app_id

    @property
    def DATA_DIR(self) -> Path:
        """IN: None (property reader).
        OUT: Local data directory Path.
        FAILS WITH: Never fails."""
        return self.data_dir


_cached_settings: Optional[Settings] = None


def load_settings(env_path: Optional[Path | str] = None) -> Settings:
    """IN: Optional path to .env file (Path or str).
    OUT: Validated Settings instance.
    FAILS WITH: ComponentError naming the missing environment variable."""
    # STEP 1: Discover and load .env file | takes: env_path | gives: environment populated
    if env_path is not None:
        target_path = Path(env_path)
        if target_path.exists():
            load_dotenv(dotenv_path=target_path, override=True)
    else:
        load_dotenv(override=False)

    trace("C0", "Checking required environment variables", required_count=len(REQUIRED_VARS))

    # STEP 2: Validate presence of each mandatory setting | takes: os.environ, REQUIRED_VARS | gives: validated variable map
    values: dict[str, str] = {}
    for var in REQUIRED_VARS:
        val = os.environ.get(var)
        if val is None or val.strip() == "":
            trace("C0", "Missing configuration setting", missing_var=var)
            raise ComponentError("C0", "load_config", f"Missing required environment variable: '{var}'")
        values[var] = val.strip()

    # STEP 3: Construct typed Settings object | takes: values | gives: Settings instance
    settings_obj = Settings(
        llm_base_url=values["LLM_BASE_URL"],
        llm_api_key=values["LLM_API_KEY"],
        llm_model=values["LLM_MODEL"],
        evolus_base_url=values["EVOLUS_BASE_URL"],
        evolus_api_key=values["EVOLUS_API_KEY"],
        evolus_app_id=values["EVOLUS_APP_ID"],
        data_dir=Path(values["DATA_DIR"]),
    )

    trace("C0", "Settings successfully loaded and validated", data_dir=str(settings_obj.data_dir))
    return settings_obj


def get_settings(env_path: Optional[Path | str] = None) -> Settings:
    """IN: Optional path to .env file.
    OUT: Cached or freshly initialized Settings instance.
    FAILS WITH: ComponentError naming the missing environment variable."""
    # STEP 1: Retrieve cached settings or invoke loader | takes: env_path, _cached_settings | gives: active Settings
    global _cached_settings
    if _cached_settings is None or env_path is not None:
        _cached_settings = load_settings(env_path)
    return _cached_settings


def get_data_dir() -> Path:
    """IN: None (reads DATA_DIR environment variable).
    OUT: Resolved data directory Path relative to project root.
    FAILS WITH: ComponentError on path resolution failure."""
    try:
        # STEP 1: Read DATA_DIR from environment with fallback | takes: os.environ | gives: raw_path
        raw_val = os.environ.get("DATA_DIR")
        raw_data_dir = raw_val.strip() if raw_val and raw_val.strip() else "data"
        raw_path = Path(raw_data_dir)

        # STEP 2: Resolve path relative to project root | takes: raw_path, __file__ | gives: resolved_path
        project_root = Path(__file__).resolve().parent.parent
        resolved_path = (project_root / raw_path).resolve()
        trace("C0", "Resolved data directory", raw=str(raw_path), resolved=str(resolved_path))

        return resolved_path
    except ComponentError:
        raise
    except Exception as exc:
        raise ComponentError("C0", "get_data_dir", f"Failed resolving data directory: {exc}") from exc


def __getattr__(name: str) -> Any:
    """IN: Attribute name requested on module.
    OUT: Module attribute or lazy-evaluated Settings instance.
    FAILS WITH: AttributeError if attribute name is unknown, or ComponentError if loading fails."""
    # STEP 1: Resolve dynamic attribute access | takes: name | gives: resolved attribute
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS/FAIL and exits).
    FAILS WITH: SystemExit on test failure."""
    # STEP 1: Verify missing variable failure behavior | takes: mock environment | gives: verified ComponentError
    saved_env = dict(os.environ)
    try:
        for var in REQUIRED_VARS:
            os.environ.pop(var, None)

        try:
            load_settings(env_path="non_existent_env_file.env")
            print("FAIL: Expected ComponentError on missing configuration")
            sys.exit(1)
        except ComponentError as err:
            if "LLM_BASE_URL" not in err.message:
                print(f"FAIL: Unexpected error message: {err.message}")
                sys.exit(1)

        # STEP 2: Verify valid settings loading | takes: populated mock environment | gives: valid Settings object
        for var in REQUIRED_VARS:
            os.environ[var] = f"mock_{var.lower()}"
        os.environ["DATA_DIR"] = "./data"

        test_settings = load_settings()
        if test_settings.LLM_MODEL != "mock_llm_model":
            print("FAIL: Setting value mismatch")
            sys.exit(1)

        # STEP 3: Verify get_data_dir() works with every other variable unset | takes: isolated environment | gives: verified Path
        for var in REQUIRED_VARS:
            os.environ.pop(var, None)

        project_root = Path(__file__).resolve().parent.parent
        expected_default = (project_root / "data").resolve()
        resolved_default = get_data_dir()
        if resolved_default != expected_default:
            print(f"FAIL: Default data dir mismatch: {resolved_default} != {expected_default}")
            sys.exit(1)

        os.environ["DATA_DIR"] = "custom_data"
        expected_custom = (project_root / "custom_data").resolve()
        resolved_custom = get_data_dir()
        if resolved_custom != expected_custom:
            print(f"FAIL: Custom data dir mismatch: {resolved_custom} != {expected_custom}")
            sys.exit(1)

        saved_cwd = Path.cwd()
        try:
            os.chdir(project_root.parent)
            resolved_from_parent = get_data_dir()
            if resolved_from_parent != expected_custom:
                print(f"FAIL: Relative resolution failed from different cwd: {resolved_from_parent}")
                sys.exit(1)
        finally:
            os.chdir(saved_cwd)

        print("PASS")
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
