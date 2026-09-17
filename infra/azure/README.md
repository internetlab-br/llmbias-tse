# Coleta em VMs Azure (Gemini, Copilot, ...)

Infra usada no `experimento_2026_08` para coletar conversas de plataformas que
travam quando acessadas de IP de datacenter. Tudo aqui foi extraido das VMs
antes de deleta-las (02/09/2026) e serve para recriar a mesma montagem.

## A ideia central: sair pelo IP de casa

As plataformas (Gemini, Copilot, ChatGPT etc.) bloqueiam faixas de IP de nuvem.
A solucao foi rodar o Chrome na VM da Azure mas fazer **todo** o trafego dele
sair por um **tunel SOCKS reverso** aberto **de dentro da rede de casa**:

```
[host residencial, rede de casa] --ssh -N -R 1080--> [VM Azure] --Chrome --proxy-server=socks5://127.0.0.1:1080--> internet
```

O host residencial e um servidor sempre ligado em casa, com o mesmo IP publico da rede domestica
maquina Windows do Julio. `ssh -R 1080` abre na VM uma porta SOCKS que sai pela
da casa, entao o Chrome da Azure aparece para as plataformas com o IP
residencial.

`start-browser.sh` e **fail-closed**: se a porta 1080 nao responder, o Chrome
nao sobe. Nunca vaza o IP da Azure. O `--proxy-bypass-list=<-loopback>` garante
que nem o loopback escapa do proxy.

## Arquivos

| Arquivo | Onde vive | O que e |
|---|---|---|
| `cloud-init.yaml` | passado no `az vm create --custom-data` | provisiona a VM inteira: pacotes, Xvfb/x11vnc/noVNC, Chrome, uv, clone do repo, os dois services |
| `vm/start-browser.sh` | `/opt/llmbias/` na VM | Xvfb + x11vnc + Chrome com CDP 9333 atras do SOCKS |
| `vm/llmbias-browser.service` | `/etc/systemd/system/` | mantem o `start-browser.sh` de pe (`Restart=always`) |
| `vm/llmbias-novnc.service` | `/etc/systemd/system/` | websockify 6080 -> VNC 5901, para ver a tela pelo navegador |
| `vm/roda_lotes.sh` | `~/llmbias-tse/` na VM | roda a coleta em lotes de 20 ate bater o alvo; para depois de 3 lotes sem progresso |
| `vm/watchdog.sh` | `~/llmbias-tse/` na VM | a cada 10 min: checa tunel, IP de saida, CDP, abas de reserva e religa o runner |
| `vm/progresso.py` | `~/llmbias-tse/` na VM | conta conversas completas (`COMPLETAS=` / `ALVO=`), usado pelos dois acima |
| `vm/verifica_ip.py`, `vm/checa_conta.py`, `vm/checa_plataforma.py`, `vm/testa_driver.py` | `~/llmbias-tse/` na VM | diagnostico: IP de saida, se a conta esta logada, se o driver captura |
| `host-residencial/llmbias-tunnel-*.service` | `~/.config/systemd/user/` no host residencial | um tunel por VM, `Restart=always` |
| `acesso-*.cmd` | Windows | abre o tunel SSH local e diz a URL do noVNC |
| `sync_gemini.sh` | Windows | faz a uniao das conversas de duas VMs da mesma plataforma, preferindo as completas |

## Recriar do zero

### 1. VM na Azure

```bash
az login   # com a conta que tem a subscription
SUB=<subscription-id>
az group create -n rg-llmbias-coleta -l brazilsouth --subscription $SUB

az vm create -g rg-llmbias-coleta -n vm-gemini --subscription $SUB \
  --image Canonical:ubuntu-24_04-lts:server:latest \
  --size Standard_D2s_v3 --os-disk-size-gb 64 \
  --admin-username azureuser --ssh-key-values ~/.ssh/llmbias_azure.pub \
  --custom-data cloud-init.yaml --nsg-rule NONE

# SSH so do IP de casa (o tunel sai de la; ninguem mais precisa entrar)
az network nsg rule create -g rg-llmbias-coleta --nsg-name vm-geminiNSG \
  -n allow-ssh-casa --priority 100 --access Allow --protocol Tcp --direction Inbound \
  --source-address-prefixes $(curl -s https://api.ipify.org) --destination-port-ranges 22
```

**Quota:** a subscription usada tinha limite **0** na familia `DASv5`.
`DSv3` tem 10 vCPU, ou seja **5 VMs `Standard_D2s_v3`** no maximo.

### 2. Tunel no host residencial

```bash
scp ~/.ssh/llmbias_azure <host>:~/.ssh/
scp host-residencial/llmbias-tunnel-gemini.service <host>:~/.config/systemd/user/
ssh <host> 'loginctl enable-linger $USER && systemctl --user daemon-reload && \
          systemctl --user enable --now llmbias-tunnel-gemini'
```

`loginctl enable-linger` e obrigatorio: sem ele os services do usuario morrem
quando a sessao SSH fecha.

### 3. Login manual nas plataformas

```cmd
acesso-gemini.cmd      :: abre o tunel; depois va em http://localhost:6080/vnc.html
```

Logue na conta pela tela remota. O perfil persiste em
`~/llmbias-tse/tmp/profile` na VM, entao e uma vez so.

### 4. Coleta

```bash
ssh -i ~/.ssh/llmbias_azure azureuser@<ip> \
  'tmux new-session -d -s coleta "~/llmbias-tse/roda_lotes.sh gemini"'
# watchdog a cada 10 min
ssh -i ~/.ssh/llmbias_azure azureuser@<ip> \
  '(crontab -l 2>/dev/null; echo "*/10 * * * * $HOME/llmbias-tse/watchdog.sh gemini") | crontab -'
```

Acompanhar: `tail -f ~/coleta_gemini.log`, `~/watchdog.log`,
`uv run python progresso.py gemini`.

### 5. Baixar os dados ANTES de destruir

```bash
scp -i ~/.ssh/llmbias_azure -r \
  azureuser@<ip>:'~/llmbias-tse/data/<run_id>' ./tmp/azure/
```

Conferir que a contagem local bate com a da VM antes de apagar qualquer coisa:

```bash
ssh -i ~/.ssh/llmbias_azure azureuser@<ip> 'ls ~/llmbias-tse/data/<run>/conversations/' | sort > /tmp/vm.txt
ls tmp/azure/pacote_<plat>/conversations | sort > /tmp/local.txt
comm -23 /tmp/vm.txt /tmp/local.txt   # tem que sair vazio
```

### 6. Destruir

```bash
az group delete -n rg-llmbias-coleta --subscription $SUB --yes
ssh <host> 'systemctl --user disable --now llmbias-tunnel-gemini llmbias-tunnel-copilot llmbias-tunnel-gemini2'
```

Deletar o **grupo** (nao so a VM) e o que zera o custo: disco, IP publico, NIC e
NSG continuam cobrando se ficarem para tras. `az vm deallocate` e a alternativa
quando se quer voltar depois preservando os perfis logados.

## Armadilhas ja pagas

- **O IP de casa muda.** Foi o que matou a coleta em 31/08: o IP residencial
  trocou, a regra de NSG (presa ao IP antigo) barrou o SSH, o tunel caiu e o
  Chrome, fail-closed, parou. Ninguem percebeu por 2 dias. Ou usar DNS dinamico
  na regra, ou o watchdog tem que alertar em algum canal que o Julio le, nao so
  num arquivo de log dentro da VM.
- **Nao rodar duas contas da mesma plataforma pelo mesmo IP de saida** sem
  pensar: as plataformas correlacionam. A `vm-gemini2` usava uma 2a conta Google
  e ficou so no eixo `integridade` para nao concorrer com a `vm-gemini`.
- **Duas VMs na mesma plataforma divergem.** Cada uma escreve so as suas
  conversas; `sync_gemini.sh` faz a uniao (preferindo a versao completa) e
  devolve para as duas. Rodar antes de contar progresso.
- **Gemini free trava sob carga** e diminuir o ritmo piora. Ver a nota do
  projeto sobre Flash-Lite throttling.
- **`--per-platform-limit 20` ja pega as proximas 20 pendentes** a cada
  chamada; o `todo` exclui as concluidas. Nao precisa aumentar o limite a cada
  lote.
