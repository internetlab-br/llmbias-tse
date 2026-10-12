"""Emendas de `recuperados.jsonl`: só na resposta e no run de onde saíram.

A chave da emenda é (conversa, turno), e ela se repete entre rodadas — o
`whatsapp_metaai_P037_integridade` da rodada 3 tem o mesmo id do da rodada 2.
Até out/2026 o `turnos_limpos` lia por padrão o arquivo da rodada 2, fixo no
código, e colaria aquelas emendas nas respostas da rodada 3.
"""

import json

from llmbias_tse import storage

CID = "whatsapp_metaai_P037_integridade"
GRAVADO = "No modelo brasileiro o voto é secreto e as"


def _run(tmp_path, nome, chars_gravados=len(GRAVADO)):
    d = tmp_path / nome
    d.mkdir()
    (d / "recuperados.jsonl").write_text(json.dumps({
        "conversation_id": CID, "turno": 1, "chars_gravados": chars_gravados,
        "chars_recuperados": 22, "continuacao": "urnas são auditadas.",
    }) + "\n", encoding="utf-8")
    return d


def _conv(resposta, **extra):
    return {"conversation_id": CID, "platform": "whatsapp_metaai",
            "turns": [{"turn": 1, "prompt": "p", "response": resposta, "ok": True}],
            **extra}


def setup_function(_):
    storage._REC_CACHE.clear()


def test_emenda_aplicada_no_proprio_run(tmp_path):
    run = _run(tmp_path, "rodada2")
    t = storage.turnos_limpos(_conv(GRAVADO, run_dir=str(run)))
    assert t[0]["response"].endswith("urnas são auditadas.")


def test_emenda_nao_entra_em_resposta_de_outro_tamanho(tmp_path):
    run = _run(tmp_path, "rodada2")
    outra = "Resposta completamente diferente da rodada 3."
    t = storage.turnos_limpos(_conv(outra, run_dir=str(run)))
    assert t[0]["response"] == outra


def test_rodada_nova_nao_le_emendas_da_rodada_2(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_ROOT", tmp_path)
    _run(tmp_path, "experimento_2026_09")      # a rodada 2, com emenda
    (tmp_path / "experimento_2026_10").mkdir()  # a rodada 3, sem emenda
    t = storage.turnos_limpos(_conv(GRAVADO, run_id="experimento_2026_10"))
    assert t[0]["response"] == GRAVADO


def test_run_id_localiza_as_emendas_da_propria_rodada(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_ROOT", tmp_path)
    _run(tmp_path, "experimento_2026_09")
    t = storage.turnos_limpos(_conv(GRAVADO, run_id="experimento_2026_09"))
    assert t[0]["response"].endswith("urnas são auditadas.")
