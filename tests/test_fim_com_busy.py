"""Texto parado com o indicador de 'gerando' na tela não é fim de resposta.

Rodada 3 (out/2026): o Claude escrevia a frase de abertura, pesquisava na web
com o texto parado, e a captura fechava ali — 136 turnos só com "Vou checar
os fatos antes de responder."
"""

from llmbias_tse import capture


def _roteiro(monkeypatch, quadros):
    """Cada sondagem consome um quadro (busy, texto); o relógio anda 1 s."""
    agora = [0.0]
    it = iter(quadros)
    atual = [next(it)]

    def avancar():
        try:
            atual[0] = next(it)
        except StopIteration:
            pass

    monkeypatch.setattr(capture.time, "time", lambda: agora[0])
    monkeypatch.setattr(capture.time, "sleep",
                        lambda s: (agora.__setitem__(0, agora[0] + 1), avancar()))
    monkeypatch.setattr(capture, "any_visible", lambda page, sels: atual[0][0])
    monkeypatch.setattr(capture, "last_text", lambda page, read: atual[0][1])


def test_texto_parado_com_busy_nao_encerra(monkeypatch):
    abertura = "Vou checar os fatos antes de responder."
    final = abertura + " Resposta completa depois da busca."
    quadros = [(True, abertura)] * 30 + [(True, final)] * 3 + [(False, final)] * 5
    _roteiro(monkeypatch, quadros)
    out = capture.wait_until_idle(None, "x", ["b"], stable_for=6.0,
                                  stable_for_busy=120.0, quiet_for=1.8,
                                  timeout=400, poll=1)
    assert out == final


def test_sem_stable_for_busy_mantem_comportamento_antigo(monkeypatch):
    abertura = "Vou checar os fatos antes de responder."
    _roteiro(monkeypatch, [(True, abertura)] * 30)
    out = capture.wait_until_idle(None, "x", ["b"], stable_for=6.0,
                                  quiet_for=1.8, timeout=400, poll=1)
    assert out == abertura
