"""Human-readable error pages for browser requests to the local dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

_MAX_CODE_LENGTH = 80

# Specific refusals raised by the dashboard routes: detail code -> (title, message).
# The detail codes are static strings chosen in source; never put user input in them.
_KNOWN_DETAILS: dict[str, tuple[str, str]] = {
    "invalid csrf token": (
        "Sessão do painel expirada",
        "O token de segurança deste formulário não corresponde ao painel atual, "
        "normalmente porque o painel foi reiniciado. Recarregue a página e repita a ação.",
    ),
    "explicit confirmation required": (
        "Confirmação necessária",
        "Esta ação só é executada depois de marcar a caixa de confirmação do formulário.",
    ),
    "automation run cannot start": (
        "A run não pôde ser iniciada",
        "O módulo pode estar desabilitado, com kill switch ativo, ou a chave de "
        "idempotência conflita com uma run existente. Confira o estado na visão geral.",
    ),
    "dispatch broker unavailable": (
        "Fila indisponível",
        "O broker local não aceitou o disparo. Verifique Redis e workers em Saúde "
        "operacional e tente de novo.",
    ),
    "approval cannot be approved": (
        "Decisão não aplicável",
        "A approval já foi decidida ou mudou de estado. Atualize a página para ver o estado atual.",
    ),
    "approval cannot be rejected": (
        "Decisão não aplicável",
        "A approval já foi decidida ou mudou de estado. Atualize a página para ver o estado atual.",
    ),
    "run cannot be cancelled": (
        "Cancelamento indisponível",
        "O estado atual da run não permite solicitar cancelamento. Atualize a página.",
    ),
    "run cannot be retried": (
        "Retentativa indisponível",
        "O estado atual da run não permite retentar. Atualize a página.",
    ),
    "automation cannot be retried": (
        "Retentativa indisponível",
        "A automação não está em condição de aceitar uma retentativa agora. Confira o "
        "módulo na visão geral.",
    ),
    "form body too large": (
        "Formulário grande demais",
        "O conteúdo enviado excede o limite aceito pelo painel.",
    ),
}
_STATUS_FALLBACKS: dict[int, tuple[str, str]] = {
    400: (
        "Requisição inválida",
        "Os dados enviados não são válidos. Recarregue a página e tente de novo.",
    ),
    403: ("Acesso recusado", "O painel recusou esta solicitação."),
    404: (
        "Não encontrado",
        "O registro ou a página pedida não existe neste painel.",
    ),
    405: ("Método não permitido", "Esta página não aceita o envio feito."),
    409: (
        "Ação indisponível no estado atual",
        "O estado atual não permite esta ação. Atualize a página e confira.",
    ),
    413: ("Formulário grande demais", "O conteúdo enviado excede o limite do painel."),
    415: ("Formato não suportado", "O painel não aceita este tipo de conteúdo."),
    422: (
        "Dados inválidos",
        "Os dados enviados não passaram na validação. Recarregue a página e tente de novo.",
    ),
    503: (
        "Serviço indisponível",
        "Uma dependência local não respondeu. Veja Saúde operacional e tente de novo.",
    ),
}
_SERVER_FALLBACK = (
    "Erro no painel",
    "O painel não conseguiu concluir esta solicitação. Consulte os logs locais.",
)
_GENERIC_FALLBACK = ("Solicitação recusada", "O painel recusou esta solicitação.")


@dataclass(frozen=True)
class ErrorPage:
    """What the error template renders; carries no request data beyond a safe back URL."""

    status_code: int
    title: str
    message: str
    code: str | None = None
    back_url: str | None = None


def wants_html(accept_header: str | None) -> bool:
    """Return True for browser navigations; media, HTMX and API callers keep JSON."""
    return accept_header is not None and "text/html" in accept_header.lower()


def safe_back_url(referer: str | None, *, host: str, current_path: str) -> str | None:
    """Return a same-host path from the Referer header, or None when it is unsafe."""
    if not referer:
        return None
    try:
        parts = urlsplit(referer)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or parts.netloc.lower() != host.lower():
        return None
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//") or path == current_path:
        return None
    if any(character in path for character in "\r\n\\"):
        return None
    return f"{path}?{parts.query}" if parts.query else path


def describe_error(
    status_code: int,
    detail: object,
    *,
    back_url: str | None = None,
) -> ErrorPage:
    """Map a status and HTTPException detail to a static pt-BR explanation."""
    code = detail if isinstance(detail, str) and detail else None
    if code is not None and len(code) > _MAX_CODE_LENGTH:
        code = None
    known = _KNOWN_DETAILS.get(code) if code is not None else None
    if known is not None:
        title, message = known
    elif status_code in _STATUS_FALLBACKS:
        title, message = _STATUS_FALLBACKS[status_code]
    elif status_code >= 500:
        title, message = _SERVER_FALLBACK
    else:
        title, message = _GENERIC_FALLBACK
    return ErrorPage(
        status_code=status_code,
        title=title,
        message=message,
        code=code,
        back_url=back_url,
    )
