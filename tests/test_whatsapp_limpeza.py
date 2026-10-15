import json

from llmbias_tse.drivers import WhatsAppMetaAI


def _driver(monkeypatch, tmp_path, a_cada=3, ok=True):
    monkeypatch.setenv("LLMBIAS_EVENTS", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SESSAO", "whatsapp_metaai.c1")
    d = WhatsAppMetaAI()
    d.limpar_a_cada = a_cada
    d.chamadas = 0

    def limpar(page):
        d.chamadas += 1
        return ok, "teste"
    d.limpar_chat = limpar
    d._garantir_chat_meta_ai = lambda page: None
    return d


def test_sem_contador_limpa_na_primeira(monkeypatch, tmp_path):
    d = _driver(monkeypatch, tmp_path)
    d._limpar_se_preciso(None)
    assert d.chamadas == 1
    assert (tmp_path / "control" / "whatsapp_metaai.c1.wa_desde_limpeza").read_text() == "1"
    reg = json.loads((tmp_path / "limpezas_whatsapp.jsonl").read_text())
    assert reg["por"] == "automatica" and reg["ok"] is True


def test_limpa_a_cada_n_conversas(monkeypatch, tmp_path):
    d = _driver(monkeypatch, tmp_path, a_cada=3)
    for _ in range(7):
        d._limpar_se_preciso(None)
    # 1ª (sem contador), depois a cada 3: conversas 1, 4, 7
    assert d.chamadas == 3


def test_falha_nao_zera_e_tenta_de_novo(monkeypatch, tmp_path):
    d = _driver(monkeypatch, tmp_path, ok=False)
    d._limpar_se_preciso(None)
    d._limpar_se_preciso(None)
    assert d.chamadas == 2


def test_zero_desliga(monkeypatch, tmp_path):
    d = _driver(monkeypatch, tmp_path, a_cada=0)
    d._limpar_se_preciso(None)
    assert d.chamadas == 0
