from llmbias_tse.drivers import WhatsAppMetaAI

AVISO = ("All AIs will be reset to their default states. All AI copies of "
         "your conversations will be deleted from Meta servers.")


def test_aviso_sozinho_vira_vazio():
    assert WhatsAppMetaAI()._sem_aviso_reset(AVISO) == ""


def test_aviso_colado_na_resposta_sai():
    assert WhatsAppMetaAI()._sem_aviso_reset(AVISO + "\npronto") == "pronto"


def test_resposta_normal_intacta():
    t = "O segundo turno será em 25 de outubro."
    assert WhatsAppMetaAI()._sem_aviso_reset(t) == t
