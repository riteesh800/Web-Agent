import os
from pathlib import Path
from typing import Optional, Tuple
from dotenv import load_dotenv


class ConfigRegistry:
    storage_root = Path(__file__).resolve().parent.parent
    ui_directory = storage_root / "frontend"
    primary_secrets_path = storage_root / ".env"

    @classmethod
    def sync_environment(cls):
        if cls.primary_secrets_path.exists():
            load_dotenv(dotenv_path=cls.primary_secrets_path, override=True)
        else:
            load_dotenv(override=True)

    @classmethod
    def lookup_secret(cls, variable_identifier: str, alternate_identifier: Optional[str] = None) -> Optional[str]:
        cls.sync_environment()
        fetched = os.getenv(variable_identifier)
        if (not fetched or not fetched.strip()) and alternate_identifier:
            fetched = os.getenv(alternate_identifier)
        return fetched.strip() if fetched else None

    @classmethod
    def read_numeric_setting(cls, setting_name: str, fallback_number: float) -> float:
        cls.sync_environment()
        raw_val = os.getenv(setting_name)
        if not raw_val:
            return fallback_number
        try:
            return float(raw_val)
        except ValueError:
            return fallback_number


class VaultManager:
    SIGNATURE_TABLE = [
        ("sk-or-v1-", "OPENROUTER_API_KEY", "OpenRouter"),
        ("gsk_", "GROQ_API_KEY", "Groq"),
        ("AIzaSy", "GOOGLE_API_KEY", "Google Gemini"),
        ("AQ.", "GOOGLE_API_KEY", "Google Gemini"),
        ("bu_", "BROWSER_USE_API_KEY", "Browser-Use Cloud"),
        ("sk-", "OPENAI_API_KEY", "OpenAI"),
    ]

    PROVIDER_VAR_MAP = {
        "openrouter": ("OPENROUTER_API_KEY", "OpenRouter"),
        "gemini": ("GOOGLE_API_KEY", "Google Gemini"),
        "groq": ("GROQ_API_KEY", "Groq"),
        "openai": ("OPENAI_API_KEY", "OpenAI"),
        "anthropic": ("ANTHROPIC_API_KEY", "Anthropic"),
    }

    @classmethod
    def identify_service_label(cls, token_string: str) -> Tuple[str, str]:
        cleaned = token_string.strip()
        for marker_prefix, var_name, friendly_label in cls.SIGNATURE_TABLE:
            if cleaned.startswith(marker_prefix):
                return var_name, friendly_label
        return "OPENROUTER_API_KEY", "OpenRouter"

    @classmethod
    def set_provider_secret(cls, provider_id: str, token_string: str) -> Tuple[bool, str, str]:
        provider_clean = (provider_id or "").strip().lower()
        token_clean = token_string.strip()
        if not token_clean:
            return False, "", "API token cannot be blank."

        if provider_clean in cls.PROVIDER_VAR_MAP:
            chosen_var, label_text = cls.PROVIDER_VAR_MAP[provider_clean]
        else:
            chosen_var, label_text = cls.identify_service_label(token_clean)

        vars_to_update = [chosen_var]
        if chosen_var == "GOOGLE_API_KEY":
            vars_to_update.append("GEMINI_API_KEY")

        target_file = ConfigRegistry.primary_secrets_path
        serialized_rows = []
        updated_vars = set()

        if target_file.exists():
            file_data = target_file.read_text(encoding="utf-8")
            for existing_row in file_data.splitlines():
                stripped_entry = existing_row.strip()
                matched = False
                for var_name in vars_to_update:
                    if stripped_entry.startswith(f"{var_name}="):
                        serialized_rows.append(f"{var_name}={token_clean}")
                        updated_vars.add(var_name)
                        matched = True
                        break
                if not matched:
                    serialized_rows.append(existing_row)

        for var_name in vars_to_update:
            if var_name not in updated_vars:
                serialized_rows.append(f"{var_name}={token_clean}")

        target_file.write_text("\n".join(serialized_rows) + "\n", encoding="utf-8")
        ConfigRegistry.sync_environment()
        return True, label_text, f"Successfully saved {label_text} credentials to .env."

    @classmethod
    def store_secret_token(cls, token_string: str) -> Tuple[bool, str, str]:
        token_clean = token_string.strip()
        if not token_clean:
            return False, "", "API token cannot be blank."

        chosen_var, label_text = cls.identify_service_label(token_clean)
        target_file = ConfigRegistry.primary_secrets_path

        serialized_rows = []
        updated_flag = False

        if target_file.exists():
            file_data = target_file.read_text(encoding="utf-8")
            for existing_row in file_data.splitlines():
                stripped_entry = existing_row.strip()
                if stripped_entry.startswith(f"{chosen_var}="):
                    serialized_rows.append(f"{chosen_var}={token_clean}")
                    updated_flag = True
                else:
                    serialized_rows.append(existing_row)

        if not updated_flag:
            serialized_rows.append(f"{chosen_var}={token_clean}")

        target_file.write_text("\n".join(serialized_rows) + "\n", encoding="utf-8")
        ConfigRegistry.sync_environment()
        return True, label_text, f"Successfully activated {label_text} credentials."
