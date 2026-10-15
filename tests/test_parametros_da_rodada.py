"""O plano manda: retomar sem flags adota desenho, balanceamento e calendário
gravados; flag explícita que contradiz o plano continua abortando.

As estações de coleta retomam só com run-id, plataforma e eixos. Até out/2026
a execução sem `--desenho-corrida` assumia o padrão (`majoritarias`), e uma
rodada planejada com `segundo_turno` (a rodada 3) abortaria em todas elas.
"""

import json

import pytest

from llmbias_tse import corridas
from llmbias_tse.conjoint_experiment import (
    _conferir_plano_compativel, _parametros_da_rodada)


def _plano(tmp_path, **kw):
    plano = {"desenho_corrida": "segundo_turno",
             "balanceamento_corrida": "cargo",
             "calendario": corridas.CALENDARIO_RODADA3.to_dict(), **kw}
    (tmp_path / "plano_coleta.json").write_text(json.dumps(plano))
    return plano


def test_sem_plano_valem_os_padroes(tmp_path):
    d, b, c = _parametros_da_rodada(tmp_path, None, None, None)
    assert (d, b, c) == (corridas.DESENHO_PADRAO,
                         corridas.BALANCEAMENTO_PADRAO,
                         corridas.CALENDARIO_2026)


def test_retomar_sem_flags_adota_o_plano(tmp_path):
    plano = _plano(tmp_path)
    d, b, c = _parametros_da_rodada(tmp_path, None, None, None)
    assert d == "segundo_turno" and b == "cargo"
    assert c == corridas.CALENDARIO_RODADA3
    # e o que foi adotado passa na conferência de compatibilidade
    _conferir_plano_compativel(plano, ["voto"], d, c, b)


def test_flag_que_contradiz_o_plano_aborta(tmp_path):
    plano = _plano(tmp_path)
    d, b, c = _parametros_da_rodada(tmp_path, "majoritarias", None, None)
    assert d == "majoritarias"
    with pytest.raises(SystemExit):
        _conferir_plano_compativel(plano, ["voto"], d, c, b)
