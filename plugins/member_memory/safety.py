"""Deterministic credential checks shared by derived memory writers."""
import re
import unicodedata

_SECRET_LABEL = re.compile(
    r'(?:api[\s_-]*key|access[\s_-]*key|client[\s_-]*secret|secret|token|'
    r'password|passwd|authorization|bearer|密码|口令|密钥|私钥|令牌|凭据)', re.I,
)
_SECRET_VALUE = re.compile(
    r'(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|'
    r'AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}'
    r'\.[A-Za-z0-9_-]{4,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)'
)

def contains_secret(value: str) -> bool:
    normalized = unicodedata.normalize('NFKC', value)
    return bool(_SECRET_LABEL.search(normalized) or _SECRET_VALUE.search(normalized))
