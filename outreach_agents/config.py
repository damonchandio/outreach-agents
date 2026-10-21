"""Settings, all from environment variables (or a .env file you load yourself). Nothing secret lives in code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _ints(name: str, default: str) -> list[int]:
    return [int(x) for x in _env(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    # The sending mailbox. Use a separate mailbox for outreach, never your main one.
    email: str = field(default_factory=lambda: _env("OUTREACH_EMAIL"))
    password: str = field(default_factory=lambda: _env("OUTREACH_PASSWORD"))
    from_name: str = field(default_factory=lambda: _env("OUTREACH_FROM_NAME"))
    smtp_host: str = field(default_factory=lambda: _env("OUTREACH_SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("OUTREACH_SMTP_PORT", "587")))   # STARTTLS
    imap_host: str = field(default_factory=lambda: _env("OUTREACH_IMAP_HOST"))
    imap_port: int = field(default_factory=lambda: int(_env("OUTREACH_IMAP_PORT", "993")))

    # Who is sending. The law asks every email to say so; nothing is sent without these.
    legal_name: str = field(default_factory=lambda: _env("OUTREACH_LEGAL_NAME"))
    company_number: str = field(default_factory=lambda: _env("OUTREACH_COMPANY_NUMBER"))
    address: str = field(default_factory=lambda: _env("OUTREACH_ADDRESS"))
    privacy_url: str = field(default_factory=lambda: _env("OUTREACH_PRIVACY_URL"))
    website: str = field(default_factory=lambda: _env("OUTREACH_WEBSITE"))
    signature: str = field(default_factory=lambda: _env("OUTREACH_SIGNATURE").replace("\\n", "\n"))
    offer_line: str = field(default_factory=lambda: _env("OUTREACH_OFFER_LINE"))   # e.g. pricing, used in follow-up 1

    # Public base URL of `outreach-agents serve` (demo pages and the opt-out page)
    public_url: str = field(default_factory=lambda: _env("OUTREACH_PUBLIC_URL", "http://localhost:8800").rstrip("/"))

    # Companies House (free key: developer.company-information.service.gov.uk)
    companies_house_key: str = field(default_factory=lambda: _env("COMPANIES_HOUSE_API_KEY"))

    # Volume. Keep it low: a new mailbox that sends hundreds a day to strangers lands in spam and gets
    # blocked. 20 a day, warmed up over a few weeks, is plenty for a small business.
    daily_cap: int = field(default_factory=lambda: int(_env("OUTREACH_DAILY_CAP", "20")))
    ramp: list[int] = field(default_factory=lambda: _ints("OUTREACH_RAMP", "5,10,15"))   # per week, then daily_cap
    send_days: list[int] = field(default_factory=lambda: _ints("OUTREACH_SEND_DAYS", "0,1,2,3,4"))   # Mon=0
    send_from: str = field(default_factory=lambda: _env("OUTREACH_SEND_FROM", "09:00"))
    send_until: str = field(default_factory=lambda: _env("OUTREACH_SEND_UNTIL", "17:00"))
    followup_days: list[int] = field(default_factory=lambda: _ints("OUTREACH_FOLLOWUP_DAYS", "3,7"))
    approve_first: int = field(default_factory=lambda: int(_env("OUTREACH_APPROVE_FIRST", "20")))

    # Stop rules
    max_bounce_rate: float = field(default_factory=lambda: float(_env("OUTREACH_MAX_BOUNCE_RATE", "0.05")))
    bounce_min_count: int = 3       # a couple of typo'd addresses never stop sending
    bounce_window: int = 100        # judged over the last N sends
    bounce_min_sends: int = 30

    # Where to look (OpenStreetMap area names, | separated)
    areas: list[str] = field(default_factory=lambda: [a.strip() for a in _env(
        "OUTREACH_AREAS", "Leeds|Manchester|Sheffield|Bristol|Nottingham").split("|") if a.strip()])

    # Language model: any OpenAI-compatible endpoint (Groq, OpenRouter, LM Studio...) or Ollama
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL").rstrip("/"))   # e.g. https://api.groq.com/openai/v1
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "qwen2.5:7b"))

    user_agent: str = field(default_factory=lambda: _env("OUTREACH_USER_AGENT", "OutreachAgentsBot/1.0"))
    data_dir: Path = field(default_factory=lambda: Path(_env("OUTREACH_DATA_DIR", "outreach-data")))

    def missing(self) -> list[str]:
        """What has to be filled in before Postman may send anything."""
        need = {"OUTREACH_EMAIL": self.email, "OUTREACH_PASSWORD": self.password,
                "OUTREACH_SMTP_HOST": self.smtp_host, "OUTREACH_LEGAL_NAME": self.legal_name,
                "OUTREACH_ADDRESS": self.address, "COMPANIES_HOUSE_API_KEY": self.companies_house_key}
        return [k for k, v in need.items() if not v]


settings = Settings()


def reload() -> Settings:
    """Re-read the environment (tests and long-running processes)."""
    global settings
    settings = Settings()
    return settings
