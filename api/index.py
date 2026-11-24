"""Painel Orion e Vanguard — Vercel (Flask). Regras: CRN (negocios87-sketch/CRN, branch main).

Orion e Vanguard são a mesma equipe (alias da Colab Orion → Vanguard, nome oficial Vanguard).
Tudo pelo mês fechado escolhido no filtro (mês + ano), horário de Brasília (R0).

Regras da CRN usadas:
  Ganhos (data de ganho): M1 · M2-B · M2-M · M3-M · T1-M · R3 · R4 · G1 · LC1 · G2 · WS1
  Reuniões (data da reunião): RC1 (feitas) · CV2 (conversão, deals únicos)
  Perdidos (data de perda): M8 · P1
  Pessoas e metas: Colab, Metas, Feriados (crn.base.fontes) · R2 (papel)

Escolhas deste painel (não são regra, vão no carimbo):
  - Closers = quem está no Vanguard na Colab do mês e NÃO tem meta de reunião (R2).
  - Quem está em FORA_DO_TOTAL aparece nas tabelas, mas sai dos totais do time e da meta
    (botão "Incluir Victor Marçal no total" no painel).
  - Card de reuniões = deals únicos com reunião feita (mesma base da CV2).
  - Mês de origem do perdido = data da última aplicação; vazia → data de criação do deal.
  - Produto e motivo de perda em texto cru (G3 está em Decisão).

Environment Variables (Vercel): PIPEDRIVE_TOKEN, URL_COLAB, URL_METAS, URL_FERIADOS, CRN_GITHUB_TOKEN.
"""
import io
import os

# ── CRN: baixada do GitHub quando o painel liga ───────────────
# A Vercel não repassa o token para o instalador do build, então a CRN não entra pelo
# requirements.txt. O painel baixa a versão atual da main (com o token, que em tempo de
# execução funciona) e carrega. Cada vez que o painel liga, pega as regras mais recentes.
# No computador, se a crn já estiver instalada (instalar.bat), usa a instalada.
def _carregar_crn():
    import importlib.util
    import shutil
    import sys
    import tarfile
    import tempfile
    if importlib.util.find_spec("crn") is not None:
        return None
    import requests
    token = (os.environ.get("CRN_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("CRN_GITHUB_TOKEN não está nas Environment Variables da Vercel.")
    r = requests.get("https://api.github.com/repos/negocios87-sketch/CRN/tarball/main",
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                     timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Não consegui baixar a CRN do GitHub (HTTP {r.status_code}). Confira o CRN_GITHUB_TOKEN.")
    base = tempfile.mkdtemp(prefix="crn_")
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        tar.extractall(base, filter="data")
    raiz = os.path.join(base, os.listdir(base)[0])          # negocios87-sketch-CRN-<commit>
    destino = os.path.join(base, "pacote", "crn")
    shutil.copytree(os.path.join(raiz, "lib"), destino)     # mesmo mapa do pyproject da CRN
    shutil.copytree(os.path.join(raiz, "config"), os.path.join(destino, "config"))
    sys.path.insert(0, os.path.join(base, "pacote"))
    return os.path.basename(raiz).rsplit("-", 1)[-1][:7]   # commit que entrou

_ERRO_CRN = None
try:
    _COMMIT_CRN = _carregar_crn()
    import crn
    if _COMMIT_CRN:
        crn.versao = lambda: _COMMIT_CRN                    # carimbo mostra o commit carregado
    from crn.base import fontes, fuso, pipedrive
    from crn.base.config import campo
    from crn.base.texto import cf, norm
    from crn.regras import (cv2_conversao_reuniao as cv2, g2_produto as g2, m1_ganho_closer as m1,
                            m2_faturamento_closer as m2, m3_atingimento_closer as m3, m6_reunioes_sdr as m6,
                            m8_deal_perdido as m8, p1_motivo_perda as p1, t1_mtd_closer as t1,
                            ws1_workshop as ws1)
    from crn.regras._atividades import do_tipo, por_responsavel      # base da RC1 (feitas)
    from crn.regras.lc1_licenciado import eh_licenciado
except Exception as _e:                                 # painel sobe e mostra o erro, em vez de cair
    _ERRO_CRN = f"{type(_e).__name__}: {_e}"
# ── fim do bloco da CRN ───────────────────────────────────────

import calendar
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

SQUAD = "Vanguard"                       # Orion vira Vanguard pelo alias da Colab
FORA_DO_TOTAL = ["Victor Marçal"]        # aparece nas tabelas, fora dos totais e da meta do time (escolha do painel)
CACHE_SEG = 600                          # 10 min; o botão "Atualizar com o Pipe" ignora o cache
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
         "setembro", "outubro", "novembro", "dezembro"]

_cache: dict = {}
_trava = threading.Lock()


# ── busca (só GET, só o mês pedido) ───────────────────────────
def _buscar(ano: int, mes: int) -> dict:
    ini = date(ano, mes, 1)
    fim = date(ano, mes, calendar.monthrange(ano, mes)[1])
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_ganhos = ex.submit(pipedrive.ganhos_do_periodo, ini, fim)
        f_perdidos = ex.submit(pipedrive.perdidos_do_periodo, ini, fim)
        f_reunioes = ex.submit(m6.carregar_periodo, ini, fim)
        f_usuarios = ex.submit(pipedrive.usuarios)
        f_funis = ex.submit(pipedrive.funis)
        f_opcoes = ex.submit(pipedrive.opcoes_campo, campo("nome_produto"))
        colab = fontes.carregar_colab(ano, mes)
        metas = fontes.carregar_metas(ano, mes)
        feriados = fontes.carregar_feriados()
        return {"ini": ini, "fim": fim, "ganhos": f_ganhos.result(), "perdidos": f_perdidos.result(),
                "reunioes": f_reunioes.result(), "usuarios": f_usuarios.result(), "funis": f_funis.result(),
                "opcoes_produto": f_opcoes.result(), "colab": colab, "metas": metas, "feriados": feriados,
                "buscado_em": fuso.agora_brasil()}


def _dados_do_mes(ano: int, mes: int, forcar: bool) -> dict:
    chave = (ano, mes)
    with _trava:
        item = _cache.get(chave)
        if item and not forcar and time.time() - item[0] < CACHE_SEG:
            return item[1]
    dados = _buscar(ano, mes)
    with _trava:
        _cache[chave] = (time.time(), dados)
    return dados


# ── cálculo ───────────────────────────────────────────────────
def _pct(a, b):
    return a / b * 100 if b else None


def _calcular(d: dict, ano: int, mes: int, incluir_fora: bool) -> dict:
    ini, fim, colab, metas, usuarios, funis = d["ini"], d["fim"], d["colab"], d["metas"], d["usuarios"], d["funis"]
    hoje = fuso.hoje_brasil()
    corrente = (ano, mes) == (hoje.year, hoje.month)
    ultimo_dia = min(fim, hoje) if corrente else fim

    # pessoas do time (Colab do mês; equipe pela M1.squad_da_pessoa)
    time_ = {p["nome_norm"]: p for p in colab if norm(m1.squad_da_pessoa(p)) == norm(SQUAD)}
    meta_por = {m["nome_norm"]: m for m in metas}
    sdrs = {nn for nn, m in meta_por.items() if m["papel"] == "sdr"}                  # R2
    closers = {nn for nn in time_ if nn not in sdrs}
    fora = {norm(n) for n in FORA_DO_TOTAL}
    conta = (lambda nn: True) if incluir_fora else (lambda nn: nn not in fora)
    uid_por_nome = {norm(n): uid for uid, n in usuarios.items()}
    nome_por_uid = {uid: norm(n) for uid, n in usuarios.items()}

    def nome_exib(nn):
        p = time_.get(nn) or meta_por.get(nn)
        return p["nome"] if p else nn.title()

    # ── GANHOS (M1, data de ganho) ──
    sep = m1.separar_ganhos(d["ganhos"], usuarios=usuarios, ini=ini, fim=fim)        # R3, R4, G1 fora
    por_closer = m1.ganhos_por_closer(sep["validos"], colab, funis, usuarios)        # LC1 fora
    deals_por_pessoa: dict = {}
    for nn, r in por_closer.items():
        ds = [x for x in r["deals"] if norm(r["squad_por_deal"].get(x.get("id"), "")) == norm(SQUAD)]
        if ds:
            deals_por_pessoa[nn] = ds
    deals_time = [x for nn, ds in deals_por_pessoa.items() if conta(nn) for x in ds]

    bruto, multi = m2.bruto(deals_time), m2.multiplicador(deals_time)

    # Workshop / Sala (WS1): utm_campaign + parcela irmã, sobre os ganhos válidos do mês
    ids_ws = ws1.ganhos_workshop(sep["validos"])
    ws = [x for x in deals_time if x.get("id") in ids_ws]
    workshop = {"bruto": m2.bruto(ws), "multi": m2.multiplicador(ws), "volume": m1.volume(ws),
                "pct_bruto": _pct(m2.bruto(ws), bruto), "pct_multi": _pct(m2.multiplicador(ws), multi)}
    meta_time = sum(meta_por[nn]["meta_financeira"] for nn in closers
                    if nn in meta_por and meta_por[nn]["papel"] == "closer" and conta(nn))

    ref = hoje if corrente else fim + timedelta(days=1)
    du = fontes.dias_uteis_periodo(ano, mes, d["feriados"], hoje=ref)                # A2

    # quem está fora do total (informação no card de meta)
    fora_info = []
    for nn in sorted(fora):
        ds = deals_por_pessoa.get(nn, [])
        if nn in time_ or ds:
            fora_info.append({"nome": nome_exib(nn), "bruto": m2.bruto(ds), "multi": m2.multiplicador(ds),
                              "volume": m1.volume(ds), "incluido": incluir_fora})

    # venda diária
    dias = []
    dia = ini
    while dia <= ultimo_dia:
        ds = [x for x in deals_time if fuso.data_brasil(x.get("won_time")) == dia]
        dias.append({"dia": dia.isoformat(), "volume": m1.volume(ds), "bruto": m2.bruto(ds),
                     "multi": m2.multiplicador(ds)})
        dia += timedelta(days=1)

    # por produto (G2, texto cru)
    prod: dict = {}
    for x in deals_time:
        k = g2.produto(x, d["opcoes_produto"]) or "(sem produto)"
        p = prod.setdefault(k, {"produto": k, "volume": 0, "bruto": 0.0, "multi": 0.0})
        p["volume"] += 1
        p["bruto"] += m2.bruto([x])
        p["multi"] += m2.multiplicador([x])
    produtos = sorted(prod.values(), key=lambda r: -r["bruto"])

    # vendedores (closers do time + quem vendeu pelo time)
    vendedores = []
    for nn in sorted(closers | set(deals_por_pessoa)):
        ds = deals_por_pessoa.get(nn, [])
        b, mu = m2.bruto(ds), m2.multiplicador(ds)
        b_ws = m2.bruto([x for x in ds if x.get("id") in ids_ws])                   # WS1
        meta = meta_por[nn]["meta_financeira"] if nn in meta_por else 0.0
        vendedores.append({"nome": nome_exib(nn), "volume": m1.volume(ds), "bruto": b, "multi": mu,
                           "meta": meta, "ating": m3.atingimento(mu, meta) if meta else None,      # M3-M
                           "mtd": t1.mtd(mu, meta, du["total"], du["passados"])["pct"] if meta else None,  # T1-M
                           "bruto_ws": b_ws, "pct_ws": _pct(b_ws, b),
                           "fora_do_total": nn in fora and not incluir_fora})
    vendedores.sort(key=lambda r: (r["fora_do_total"], -r["multi"]))

    # ── REUNIÕES (RC1 feitas, data da reunião) + CV2 ──
    reu = d["reunioes"]
    closer_ids = {uid_por_nome[nn] for nn in closers if nn in uid_por_nome}
    feitas = do_tipo(reu["atividades"], ["reuniao"], ini, fim, so_feitas=True,
                     responsaveis=closer_ids, sem_ia=True)
    status_atual = {x["id"]: x.get("status") for x in reu["deals"]}
    por_resp = por_responsavel(feitas)
    conversao = []
    for uid in closer_ids:
        nn = nome_por_uid.get(uid)
        c = cv2.conversao(por_resp.get(uid, []), status_atual)
        conversao.append({"nome": nome_exib(nn), "reunioes": c["reunioes"], "ganhas": c["ganhas"],
                          "pct": c["pct"], "fora_do_total": nn in fora and not incluir_fora})
    conversao.sort(key=lambda r: (r["fora_do_total"], -r["reunioes"]))
    feitas_time = [a for uid, lst in por_resp.items() if conta(nome_por_uid.get(uid)) for a in lst]
    cv2_time = cv2.conversao(feitas_time, status_atual)

    # ── PERDIDOS (M8, data de perda) ──
    perd = []
    for x in m8.perdidos(d["perdidos"], ini, fim, eixo="perda"):
        if eh_licenciado(x, funis):
            continue
        nn = norm(m1.nome_dono(x, usuarios))
        if nn in time_ and conta(nn):            # dono no time (closer ou SDR)
            perd.append(x)

    perd_dia = []
    dia = ini
    while dia <= ultimo_dia:
        perd_dia.append({"dia": dia.isoformat(),
                         "volume": sum(1 for x in perd if fuso.data_brasil(x.get("lost_time")) == dia)})
        dia += timedelta(days=1)

    mot: dict = {}
    for x in perd:
        k = p1.motivo(x)
        mot[k] = mot.get(k, 0) + 1
    motivos = sorted(({"motivo": k, "volume": v, "pct": _pct(v, len(perd))} for k, v in mot.items()),
                     key=lambda r: -r["volume"])

    # mês de origem: data da última aplicação; vazia → criação
    def mes_menos(n):
        a, m = ano, mes - n
        while m <= 0:
            a, m = a - 1, m + 12
        return a, m
    rot = [f"{MESES[mes_menos(i)[1] - 1].capitalize()}/{mes_menos(i)[0]}" for i in range(3)]
    faixas = {r: 0 for r in rot}
    faixa_antes = f"Antes de {MESES[mes_menos(2)[1] - 1]}/{mes_menos(2)[0]}"
    faixas[faixa_antes] = 0
    pela_criacao = 0
    for x in perd:
        ap, _ = fuso.ultima_aplicacao(cf(x, campo("ultima_aplicacao")))
        if ap is None:
            ap = fuso.data_brasil(x.get("add_time"))
            pela_criacao += 1
        if ap is None:
            faixas[faixa_antes] += 1
            continue
        dist = (ano - ap.year) * 12 + (mes - ap.month)
        faixas[rot[max(dist, 0)] if dist < 3 else faixa_antes] += 1
    origem = [{"faixa": k, "volume": v, "pct": _pct(v, len(perd))} for k, v in faixas.items()]

    return {
        "mes": mes, "ano": ano, "rotulo_mes": f"{MESES[mes - 1].capitalize()}/{ano}",
        "cards": {"bruto": bruto, "multi": multi, "meta": meta_time, "ating": m3.atingimento(multi, meta_time),
                  "reunioes": cv2_time["reunioes"], "reunioes_ganhas": cv2_time["ganhas"],
                  "volume": m1.volume(deals_time), "perdidos": len(perd), "fora": fora_info},
        "workshop": workshop,
        "dias": dias, "produtos": produtos, "vendedores": vendedores, "conversao": conversao,
        "perdidos_dia": perd_dia, "motivos": motivos, "origem": origem, "origem_pela_criacao": pela_criacao,
        "du": du,
        "carimbo": {"crn": crn.versao(), "buscado_em": d["buscado_em"].strftime("%d/%m/%Y %H:%M"),
                    "fora_incluido": incluir_fora},
    }


# ── rotas ─────────────────────────────────────────────────────
PAGINA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")


@app.route("/")
def pagina():
    return send_file(PAGINA)


@app.route("/crn-versao")
def crn_versao():
    if _ERRO_CRN:
        return jsonify({"versao": None, "erro": _ERRO_CRN}), 500
    return jsonify({"versao": crn.versao()})


@app.route("/api/index")
@app.route("/api/dados")
def dados():
    if request.args.get("acao") == "versao" or "crn-versao" in request.path:
        return crn_versao()
    if _ERRO_CRN:
        return jsonify({"erro": _ERRO_CRN}), 500
    hoje = fuso.hoje_brasil()
    try:
        ano = int(request.args.get("ano", hoje.year))
        mes = int(request.args.get("mes", hoje.month))
    except ValueError:
        return jsonify({"erro": "Mês ou ano inválido."}), 400
    if not 1 <= mes <= 12 or (ano, mes) > (hoje.year, hoje.month):
        return jsonify({"erro": "Escolha um mês até o atual."}), 400
    forcar = request.args.get("atualizar") == "1"
    incluir = request.args.get("incluir_fora") == "1"
    try:
        d = _dados_do_mes(ano, mes, forcar)
        return jsonify(_calcular(d, ano, mes, incluir))
    except Exception as e:                              # erro visível, nunca "zero"
        return jsonify({"erro": f"{type(e).__name__}: {e}"}), 500


@app.route("/<path:caminho>")
def outro(caminho):
    if "crn-versao" in caminho:
        return crn_versao()
    return jsonify({"erro": "Rota não encontrada."}), 404
