"""De qual IP as chamadas saem.

Não é curiosidade de infraestrutura: as plataformas tratam IP residencial e
IP de datacenter de forma diferente — CAPTCHA, verificação de segurança,
limite de uso e, em alguns casos, a própria disposição de responder. Uma
auditoria que não registra de onde falou não é reproduzível, e o leitor do
relatório não tem como julgar se o comportamento observado vale para um
usuário comum ou para um robô visto como robô.

Medido de DENTRO do container, que compartilha a pilha de rede do Chrome da
estação: é o mesmo NAT e a mesma rota. A ressalva honesta é que isto mede o
caminho do processo, não o do navegador; só divergiriam se o Chrome usasse
proxy próprio, o que a coleta não configura.

Medido UMA vez por processo e reaproveitado. Um lote leva horas e faz uma
chamada externa por conversa não seria de graça — e, mais importante, um
serviço de eco fora do ar não pode derrubar a coleta. Falha devolve None, e
o campo fica vazio na base em vez de inventado.
"""

from __future__ import annotations

import json
import os
import urllib.request

# Em ordem de preferência: o primeiro devolve também a organização, que é o
# que distingue "banda larga residencial" de "nuvem" no relatório.
_SERVICOS = (
    ("https://ipinfo.io/json", ("ip", "org")),
    ("https://api.ipify.org?format=json", ("ip", None)),
    ("https://ifconfig.co/json", ("ip", "asn_org")),
)

_cache: dict | None = None


def _consultar(url: str, campos: tuple, timeout: float) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "llmbias-tse"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    ip = d.get(campos[0])
    if not ip:
        return None
    org = d.get(campos[1]) if campos[1] else None
    return {"ip": ip, "org": org, "fonte": url}


def ip_saida(timeout: float = 6.0, forcar: bool = False) -> dict | None:
    """`{"ip", "org", "fonte"}` do IP de saída, ou None se não der para medir.

    `LLMBIAS_IP_SAIDA` sobrescreve a consulta — útil quando a rede da coleta
    não tem saída para os serviços de eco, ou para fixar o valor num teste.
    """
    global _cache
    if _cache is not None and not forcar:
        return _cache
    declarado = os.environ.get("LLMBIAS_IP_SAIDA")
    if declarado:
        _cache = {"ip": declarado, "org": os.environ.get("LLMBIAS_IP_ORG"),
                  "fonte": "LLMBIAS_IP_SAIDA"}
        return _cache
    for url, campos in _SERVICOS:
        try:
            r = _consultar(url, campos, timeout)
        except Exception:
            continue
        if r:
            _cache = r
            return _cache
    return None
