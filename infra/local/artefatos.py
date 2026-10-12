"""Empacota os artefatos da coleta em um zip por PLATAFORMA × EIXO.

Artefato é o HTML e o PNG de cada turno, no momento da captura. Não é dado de
análise, mas foi o que salvou a rodada 1: é com ele que se recupera mensagem
quebrada e se extraem os LINKS das fontes que o modelo citou, que a captura de
texto não guarda.

Um zip só teria dezenas de GB e seria inviável de subir e de baixar. Por
plataforma × eixo dá ~1 GB por arquivo, e quem precisa de uma perna baixa só
ela.

    uv run python infra/local/artefatos.py
    uv run python infra/local/artefatos.py --plataformas claude whatsapp_metaai
"""

from __future__ import annotations

import argparse
import collections
import shutil
import subprocess
from pathlib import Path


def _tamanho(caminhos) -> int:
    return sum(f.stat().st_size for c in caminhos for f in c.rglob("*")
               if f.is_file())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/experimento_2026_09")
    ap.add_argument("--saida", default="data/artefatos")
    ap.add_argument("--plataformas", nargs="*", default=None)
    ap.add_argument("--eixos", nargs="*",
                    default=["voto", "integridade", "genero"])
    args = ap.parse_args()
    run = Path(args.run_dir)
    art = run / "artifacts"
    saida = Path(args.saida)
    saida.mkdir(parents=True, exist_ok=True)

    # Agrupa pelos nomes das pastas, que são os conversation_id
    # (<plataforma>_<perfil>_<eixo>). Split pela DIREITA: `google_aimode` e
    # `whatsapp_metaai` têm underscore no nome.
    grupos = collections.defaultdict(list)
    for d in sorted(art.iterdir()):
        if not d.is_dir():
            continue
        try:
            plat, _perfil, eixo = d.name.rsplit("_", 2)
        except ValueError:
            continue
        grupos[(plat, eixo)].append(d)

    alvos = [k for k in sorted(grupos)
             if (not args.plataformas or k[0] in args.plataformas)
             and k[1] in args.eixos]
    print(f"{len(alvos)} pacotes a gerar\n")
    total = 0
    for plat, eixo in alvos:
        convs = grupos[(plat, eixo)]
        bruto = _tamanho(convs) / 1e9
        destino = saida / f"artefatos_{plat}_{eixo}"
        if destino.exists():
            shutil.rmtree(destino)
        destino.mkdir()
        for c in convs:
            (destino / c.name).symlink_to(c.resolve())
        # `zip -y` guarda o link; queremos o conteúdo, então `-r` sem `-y`
        # seguindo os links. `-q` porque são milhares de arquivos.
        # Caminho ABSOLUTO: o `zip` roda com `cwd` na pasta de links, e um
        # caminho relativo criaria o arquivo dentro dela (ou falharia).
        zipe = Path(f"{destino}.zip").resolve()
        zipe.unlink(missing_ok=True)
        subprocess.run(["zip", "-r", "-q", "-1", str(zipe), "."],
                       cwd=destino, check=True)
        shutil.rmtree(destino)
        tam = zipe.stat().st_size / 1e9
        total += tam
        print(f"  {plat:18s} {eixo:12s} {len(convs):3d} conversas · "
              f"{bruto:.1f} GB -> {tam:.2f} GB  {zipe.name}")
    print(f"\ntotal empacotado: {total:.1f} GB em {saida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
