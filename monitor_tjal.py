#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor das Câmaras Cíveis do TJ/AL — engine.

Subcomandos:
  coletar    Busca os acórdãos das câmaras cíveis no cjsg (e-SAJ TJAL) por janela
             de DATA DE JULGAMENTO e grava CSV bruto (com ementa inline).
  classificar  Classifica cada acórdão (classe / área / tema) de forma determinística
             (mapa de assunto + léxico). Marca residual p/ leitura de ementa pelo modelo.
  agregar    Percentuais de tema por câmara + recorte por classe + tendência; grava
             registro datado e gera a mensagem (tabela por câmara) pro Slack.

Fonte (validada): https://www2.tjal.jus.br/cjsg/
  busca:    resultadoCompleta.do   (GET, params do formulário de busca avançada)
  página N: trocaDePagina.do?pagina=N&tipoDeDecisao=A   (mesma sessão, 20/página)
  PDF:      getArquivo.do?cdAcordao=<cd>&cdForo=<foro>

Sem dependências além de `requests` (parser é regex sobre HTML já caracterizado).
"""
import argparse
import csv
import datetime as dt
import html
import http.cookiejar
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www2.tjal.jus.br/cjsg"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAGE_SIZE = 20

# Órgãos cíveis de 2º grau do TJ/AL (códigos secoesTreeSelection.values).
ORGAOS_CIVEIS = {
    "0-1":  "1ª Câmara Cível",
    "0-2":  "2ª Câmara Cível",
    "0-13": "3ª Câmara Cível",
    "0-16": "4ª Câmara Cível",
    "0-4":  "Seção Especializada Cível",
}
ORGAOS_EXEC_FISCAL = {
    "166-2": "1ª Câmara - Execução Fiscal",
    "166-3": "2ª Câmara - Execução Fiscal",
    "166-4": "3ª Câmara - Execução Fiscal",
    "166-7": "4ª Câmara - Execução Fiscal",
}

CAMPOS = [
    "orgao_codigo", "orgao", "numero", "cd_acordao", "cd_foro",
    "classe", "assunto", "relator", "comarca",
    "data_julgamento", "data_registro", "data_publicacao",
    "ementa", "url_pdf",
]


# ----------------------------------------------------------------------------- #
# Coleta
# ----------------------------------------------------------------------------- #
class _Cliente:
    """Sessão HTTP stdlib (urllib) com cookiejar — sem dependências externas."""

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def get(self, url, params=None, referer=None, timeout=60):
        if params:
            url = url + "?" + urllib.parse.urlencode(params, encoding="utf-8")
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Accept-Language", "pt-BR,pt;q=0.9")
        if referer:
            req.add_header("Referer", referer)
        with self.opener.open(req, timeout=timeout) as r:
            raw = r.read()
        return raw.decode("utf-8", errors="replace")


def _novo_session():
    s = _Cliente()
    s.get(f"{BASE}/consultaCompleta.do", timeout=30)  # seat cookies
    return s


def _buscar(session, orgao_codigo, dt_ini, dt_fim):
    """Dispara a busca de um órgão e devolve (html_pagina1, total)."""
    params = {
        "dados.buscaInteiroTeor": "",
        "dados.pesquisarComSinonimos": "S",
        "dados.buscaEmenta": "",
        "dados.nuProcOrigem": "",
        "dados.nuRegistro": "",
        "agenteSelectedEntitiesList": "",
        "contadoragente": "0",
        "contadorMaioragente": "0",
        "classesTreeSelection.values": "",
        "classesTreeSelection.text": "",
        "assuntosTreeSelection.values": "",
        "assuntosTreeSelection.text": "",
        "comarcaSelectedEntitiesList": "",
        "secoesTreeSelection.values": orgao_codigo,
        "secoesTreeSelection.text": ORGAOS_CIVEIS.get(orgao_codigo, ""),
        "dados.dtJulgamentoInicio": dt_ini,
        "dados.dtJulgamentoFim": dt_fim,
        "tipoDecisaoSelecionados": "A",          # Acórdãos
        "dados.origensSelecionadas": "T",        # 2º grau
        "dados.ordenarPor": "dtPublicacao",
    }
    text = session.get(f"{BASE}/resultadoCompleta.do", params=params,
                       referer=f"{BASE}/consultaCompleta.do", timeout=60)
    total = 0
    m = re.search(r'id="totalResultadoAba-A"[^>]*value="(\d+)"', text)
    if m:
        total = int(m.group(1))
    return text, total


def _trocar_pagina(session, pagina):
    return session.get(f"{BASE}/trocaDePagina.do",
                       params={"pagina": pagina, "tipoDeDecisao": "A"},
                       referer=f"{BASE}/resultadoCompleta.do", timeout=60)


def _strip(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _split_classe_assunto(texto, classes_conhecidas):
    """Separa 'Classe / Assunto' usando a lista de classes conhecidas (match mais longo)."""
    texto = texto.strip()
    melhor = None
    for c in classes_conhecidas:
        if texto.lower().startswith(c.lower()) and (melhor is None or len(c) > len(melhor)):
            melhor = c
    if melhor:
        resto = texto[len(melhor):].lstrip(" /").strip()
        return melhor.strip(), resto
    # fallback: primeira " / "
    if " / " in texto:
        a, b = texto.split(" / ", 1)
        return a.strip(), b.strip()
    return texto, ""


def _parse_pagina(html_pagina, orgao_codigo, orgao_nome, classes_conhecidas):
    """Extrai as linhas de resultado (até 20) de uma página."""
    idxs = [m.start() for m in re.finditer(r'class="fundocinza1"', html_pagina)]
    if not idxs:
        return []
    # bound do último bloco: até o fim da tabela de resultados
    fim = html_pagina.find('id="paginacaoInferior', idxs[-1])
    if fim < 0:
        fim = len(html_pagina)
    bordas = idxs + [fim]
    linhas = []
    for i in range(len(idxs)):
        blk = html_pagina[bordas[i]:bordas[i + 1]]

        m = re.search(r'cdAcordao="(\d+)"\s+cdForo="(\d+)"\s*>\s*([\d.\-]+)\s*</a>', blk)
        if not m:
            continue
        cd_acordao, cd_foro, numero = m.group(1), m.group(2), m.group(3).strip()

        clean = _strip(blk)

        ca = re.search(r"Classe/Assunto:\s*(.+?)\s*(?:Relator\s*\(a\)|Relator:|Comarca:|Órgão julgador:)", clean)
        classe, assunto = "", ""
        if ca:
            classe, assunto = _split_classe_assunto(ca.group(1), classes_conhecidas)

        relator = (re.search(r"Relator\s*\(a\):\s*(.+?)\s*;", clean) or [None, ""])[1] \
            if re.search(r"Relator\s*\(a\):\s*(.+?)\s*;", clean) else ""
        m_rel = re.search(r"Relator\s*\(a\):\s*(.+?)\s*;", clean)
        relator = m_rel.group(1).strip() if m_rel else ""
        m_com = re.search(r"Comarca:\s*(.+?)\s*;", clean)
        comarca = m_com.group(1).strip() if m_com else ""
        m_org = re.search(r"Órgão julgador:\s*(.+?)\s*;", clean)
        orgao_txt = m_org.group(1).strip() if m_org else orgao_nome
        m_dj = re.search(r"Data do julgamento:\s*(\d{2}/\d{2}/\d{4})", clean)
        data_julg = m_dj.group(1) if m_dj else ""
        m_dr = re.search(r"Data de registro:\s*(\d{2}/\d{2}/\d{4})", clean)
        data_reg = m_dr.group(1) if m_dr else ""
        m_dp = re.search(r"Data de publicação:\s*(\d{2}/\d{2}/\d{4})", clean)
        data_pub = m_dp.group(1) if m_dp else ""

        # ementa: texto entre o número e "Classe/Assunto:"
        ementa = ""
        pos_num = clean.find(numero)
        pos_ca = clean.find("Classe/Assunto:")
        if pos_num >= 0 and pos_ca > pos_num:
            ementa = clean[pos_num + len(numero):pos_ca]
            ementa = re.sub(r"^\s*(Ementa:?|-)\s*", "", ementa).strip()

        linhas.append({
            "orgao_codigo": orgao_codigo,
            "orgao": orgao_txt or orgao_nome,
            "numero": numero,
            "cd_acordao": cd_acordao,
            "cd_foro": cd_foro,
            "classe": classe,
            "assunto": assunto,
            "relator": relator,
            "comarca": comarca,
            "data_julgamento": data_julg,
            "data_registro": data_reg,
            "data_publicacao": data_pub,
            "ementa": ementa,
            "url_pdf": f"{BASE}/getArquivo.do?cdAcordao={cd_acordao}&cdForo={cd_foro}",
        })
    return linhas


def _data(d):
    """dd/mm/aaaa -> tupla comparável (aaaa, mm, dd); '' -> None."""
    try:
        dd, mm, yy = d.split("/")
        return (int(yy), int(mm), int(dd))
    except Exception:
        return None


def coletar(dt_ini, dt_fim, orgaos, classes_conhecidas, pausa=0.5, verbose=True,
            filtrar_julgamento=True):
    todas = []
    for cod in orgaos:
        nome = ORGAOS_CIVEIS.get(cod) or ORGAOS_EXEC_FISCAL.get(cod, cod)
        s = _novo_session()
        html1, total = _buscar(s, cod, dt_ini, dt_fim)
        n_pag = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if verbose:
            print(f"[{nome}] total={total}  páginas={n_pag}", file=sys.stderr)
        linhas = _parse_pagina(html1, cod, nome, classes_conhecidas)
        todas.extend(linhas)
        for p in range(2, n_pag + 1):
            time.sleep(pausa)
            hp = _trocar_pagina(s, p)
            linhas = _parse_pagina(hp, cod, nome, classes_conhecidas)
            todas.extend(linhas)
            if verbose and p % 10 == 0:
                print(f"  ... pág {p}/{n_pag}", file=sys.stderr)
    if filtrar_julgamento:
        # A busca do cjsg deixa passar acórdãos julgados fora da janela (julgados antes,
        # apenas publicados agora). Mantém só os JULGADOS dentro de [dt_ini, dt_fim].
        ini, fim = _data(dt_ini), _data(dt_fim)
        antes = len(todas)
        todas = [r for r in todas
                 if (_data(r["data_julgamento"]) is None) or (ini and fim and ini <= _data(r["data_julgamento"]) <= fim)]
        descartados = antes - len(todas)
        if verbose and descartados:
            print(f"[filtro] {descartados} acórdãos julgados fora da janela descartados "
                  f"(publicados agora, julgados antes) — restam {len(todas)}", file=sys.stderr)
    return todas


def _gravar_csv(linhas, caminho):
    os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        for ln in linhas:
            w.writerow({k: ln.get(k, "") for k in CAMPOS})


# Classes conhecidas-semente (para separar classe×assunto). Cresce via taxonomia.yaml.
CLASSES_SEED = [
    "Apelação / Remessa Necessária",
    "Apelação Cível",
    "Apelação Criminal",
    "Agravo de Instrumento Cível",
    "Agravo Interno Cível",
    "Agravo Interno em Apelação Cível",
    "Embargos de Declaração Cível",
    "Embargos Infringentes e de Nulidade",
    "Mandado de Segurança Cível",
    "Remessa Necessária Cível",
    "Conflito de Competência Cível",
    "Ação Rescisória Cível",
    "Habeas Corpus Cível",
    "Reclamação Cível",
    "Incidente de Resolução de Demandas Repetitivas",
    "Tutela Provisória",
    "Tutela Antecipada Antecedente",
]


def _cmd_coletar(args):
    if args.dias:
        hoje = dt.date.today()
        dt_fim = hoje.strftime("%d/%m/%Y")
        dt_ini = (hoje - dt.timedelta(days=args.dias)).strftime("%d/%m/%Y")
    else:
        dt_ini, dt_fim = args.inicio, args.fim
    orgaos = list(ORGAOS_CIVEIS.keys())
    if args.exec_fiscal:
        orgaos += list(ORGAOS_EXEC_FISCAL.keys())
    if args.orgaos:
        orgaos = args.orgaos.split(",")
    print(f"Coletando câmaras cíveis TJ/AL — julgamento {dt_ini} a {dt_fim}", file=sys.stderr)
    linhas = coletar(dt_ini, dt_fim, orgaos, CLASSES_SEED, pausa=args.pausa)
    _gravar_csv(linhas, args.out)
    print(f"OK: {len(linhas)} acórdãos -> {args.out}", file=sys.stderr)


# ----------------------------------------------------------------------------- #
# Classificação (determinística; resíduo -> leitura de ementa pelo modelo)
# ----------------------------------------------------------------------------- #
import json

CAMPOS_CLASS = CAMPOS + ["classe_curta", "area", "tema", "subtema", "incidentes", "precisa_llm"]


def carregar_taxonomia(caminho=None):
    if caminho is None:
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "taxonomia.json")
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)


def _classe_curta(classe, tax):
    if classe in tax.get("classe_norm", {}):
        return tax["classe_norm"][classe]
    c = re.sub(r"\s+Cível$", "", classe).strip()
    return c or classe


def _achar_area(texto, tax):
    t = texto.lower()
    for area, kws in tax["area_keywords"]:
        for kw in kws:
            if kw in t:
                return area
    return ""


def _tema(assunto, tax):
    a = assunto.lower()
    for sub, canon in tax.get("tema_merge", {}).items():
        if sub in a:
            return canon
    return assunto.strip()


def _achar_subtema(area, assunto, ementa, tax):
    """Recorta uma área em sub-blocos lidos na EMENTA INTEIRA (não só assunto).
    Ordenado, 1º match vence; '' se a área não tem recorte; 'Outros' se nenhum casa."""
    regras = tax.get("subtemas_ementa", {}).get(area)
    if not regras:
        return ""
    t = (assunto + " " + ementa).lower()
    for label, kws in regras:
        for kw in kws:
            if kw in t:
                return label
    return "Outros"


def _nucleo_ementa(ementa):
    """Trecho que ENUNCIA o litígio (CASO EM EXAME + QUESTÃO EM DISCUSSÃO), cortando
    fora RAZÕES DE DECIDIR / TESE / DISPOSITIVO — onde o incidente costuma aparecer só
    como consequência (ex.: 'condeno em honorários'). Formato CNJ novo (100% têm
    'CASO EM EXAME'). Fallback: ementa inteira se a estrutura não for encontrada."""
    if not ementa:
        return ""
    mi = re.search(r"caso\s+em\s+exame", ementa, re.I)
    ini = mi.start() if mi else 0
    mf = re.search(r"raz[õo]es\s+de\s+decidir|tese\s+de\s+julgamento|\bdispositivo\b",
                   ementa[ini:], re.I)
    fim = ini + mf.start() if mf else len(ementa)
    nucleo = ementa[ini:fim].strip()
    return nucleo if len(nucleo) >= 30 else ementa


def _achar_incidentes(assunto, ementa, tax):
    """Eixo TRANSVERSAL e MULTIVALORADO: devolve TODOS os incidentes processuais que
    casam no núcleo do litígio (assunto + CASO/QUESTÃO), juntados por '|'. '' se nenhum."""
    defs = tax.get("incidentes_processuais", [])
    if not defs:
        return ""
    t = (assunto + " " + _nucleo_ementa(ementa)).lower()
    achados = []
    for label, kws in defs:
        if any(kw in t for kw in kws):
            achados.append(label)
    return "|".join(achados)


def classificar_linha(row, tax):
    classe_curta = _classe_curta(row.get("classe", ""), tax)
    assunto = row.get("assunto", "")
    # área: tenta pelo assunto (classificação oficial); fallback pela ementa
    area = _achar_area(assunto, tax) or _achar_area(row.get("ementa", "")[:600], tax)
    tema = _tema(assunto, tax) if assunto else ""
    # subtema: recorte fino lido na ementa inteira (só p/ áreas com subtemas_ementa)
    subtema = _achar_subtema(area, assunto, row.get("ementa", ""), tax) if area else ""
    # incidentes: eixo transversal lido no núcleo do litígio (independe da área)
    incidentes = _achar_incidentes(assunto, row.get("ementa", ""), tax)
    precisa_llm = "1" if (not area or not tema) else ""
    return classe_curta, area, tema, subtema, incidentes, precisa_llm


def _cmd_classificar(args):
    tax = carregar_taxonomia(args.taxonomia)
    rows = list(csv.DictReader(open(args.inp, encoding="utf-8")))
    n_llm = 0
    for r in rows:
        cc, area, tema, subtema, incidentes, precisa = classificar_linha(r, tax)
        (r["classe_curta"], r["area"], r["tema"], r["subtema"],
         r["incidentes"], r["precisa_llm"]) = cc, area, tema, subtema, incidentes, precisa
        if precisa:
            n_llm += 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_CLASS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CAMPOS_CLASS})
    pct = (100.0 * n_llm / len(rows)) if rows else 0
    print(f"OK: {len(rows)} classificados | resíduo p/ Sonnet: {n_llm} ({pct:.1f}%) -> {args.out}",
          file=sys.stderr)


# ----------------------------------------------------------------------------- #
# Agregação + registro datado + mensagem Slack
# ----------------------------------------------------------------------------- #
import collections

# Ordem fixa das câmaras na saída (órgãos não listados vão ao fim, alfabético).
ORDEM_CAMARAS = ["1ª Câmara Cível", "2ª Câmara Cível", "3ª Câmara Cível",
                 "4ª Câmara Cível", "Seção Especializada Cível"]


def _milhar(n):
    return f"{n:,}".replace(",", ".")


def _dist(rows, campo, top=None):
    n = len(rows)
    cnt = collections.Counter((r.get(campo) or "—") for r in rows)
    itens = [{"rotulo": k, "n": v, "pct": round(100.0 * v / n, 1)} for k, v in cnt.most_common()]
    return itens[:top] if top else itens


def _ordena_camaras(nomes):
    def chave(nm):
        return (ORDEM_CAMARAS.index(nm) if nm in ORDEM_CAMARAS else 99, nm)
    return sorted(nomes, key=chave)


def _carregar_tendencia(base_dir, rotulo_atual):
    """Registro datado anterior mais recente -> (rotulo, pct por área, pct por subtema, pct por incidente)."""
    if not base_dir or not os.path.isdir(base_dir):
        return None, None, None, None
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    cands = sorted([d for d in os.listdir(base_dir)
                    if pat.match(d) and d != rotulo_atual
                    and os.path.isfile(os.path.join(base_dir, d, "resumo.json"))])
    if not cands:
        return None, None, None, None
    anterior = cands[-1]
    try:
        prev = json.load(open(os.path.join(base_dir, anterior, "resumo.json"), encoding="utf-8"))
    except Exception:
        return None, None, None, None
    geral = {i["rotulo"]: i["pct"] for i in prev.get("geral", {}).get("areas", [])}
    destaques = {d["area"]: {i["rotulo"]: i["pct"] for i in d.get("subtemas", [])}
                 for d in prev.get("destaques", [])}
    incidentes = {i["rotulo"]: i["pct"] for i in prev.get("incidentes_proc", [])}
    return anterior, geral, destaques, incidentes


def _camara(r):
    """Câmara canônica a partir do código do órgão buscado (agrupamento determinístico)."""
    cod = r.get("orgao_codigo", "")
    return ORGAOS_CIVEIS.get(cod) or ORGAOS_EXEC_FISCAL.get(cod) or (r.get("orgao") or "—")


def _incidencia(rows):
    """Eixo transversal: % de acórdãos em que cada incidente processual aparece.
    MULTIVALORADO — um acórdão conta em vários; NÃO soma 100%. Ordenado por frequência."""
    n = len(rows)
    cnt = collections.Counter()
    for r in rows:
        for lab in (r.get("incidentes") or "").split("|"):
            if lab:
                cnt[lab] += 1
    return [{"rotulo": k, "n": v, "pct": round(100.0 * v / n, 1)} for k, v in cnt.most_common()]


def _destaques(rows, tax):
    """Para cada área com 'subtemas_ementa', recorta os acórdãos dela por subtema.
    pct = % DENTRO da área; pct_pauta = % da pauta total."""
    out = []
    total = len(rows)
    for area in tax.get("subtemas_ementa", {}):
        sub = [r for r in rows if (r.get("area") or "") == area]
        if not sub:
            continue
        out.append({
            "area": area,
            "total": len(sub),
            "pct_pauta": round(100.0 * len(sub) / total, 1) if total else 0.0,
            "subtemas": _dist(sub, "subtema"),
        })
    return out


def agregar(rows, rotulo, janela, base_dir=None, gerado_em=None, tax=None):
    camaras = _ordena_camaras({_camara(r) for r in rows})
    por_camara = {}
    for cam in camaras:
        sub = [r for r in rows if _camara(r) == cam]
        por_camara[cam] = {
            "total": len(sub),
            "areas": _dist(sub, "area"),
            "classes": _dist(sub, "classe_curta"),
            "temas": _dist(sub, "tema", top=15),
            "incidentes": _incidencia(sub),
        }
    resumo = {
        "rotulo": rotulo,
        "gerado_em": gerado_em or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "janela": janela,
        "total": len(rows),
        "orgaos": camaras,
        "por_camara": por_camara,
        "geral": {
            "areas": _dist(rows, "area"),
            "classes": _dist(rows, "classe_curta"),
            "temas": _dist(rows, "tema", top=20),
        },
        "destaques": _destaques(rows, tax) if tax else [],
        "incidentes_proc": _incidencia(rows),
    }
    anterior, prev_geral, prev_destaques, prev_incidentes = _carregar_tendencia(base_dir, rotulo)
    resumo["tendencia_vs"] = anterior
    return resumo, prev_geral, prev_destaques, prev_incidentes


def _tabela_txt(itens, base_label="matéria"):
    linhas = [f"{'%':>5}  {'n':>4}  {base_label}"]
    for it in itens:
        linhas.append(f"{it['pct']:>5.1f}  {it['n']:>4}  {it['rotulo']}")
    return "\n".join(linhas)


def _delta_pp(pct, prev_map, rotulo):
    """String '  (+x.x p.p.)' se houver registro anterior; '' caso contrário."""
    if prev_map and rotulo in prev_map:
        d = round(pct - prev_map[rotulo], 1)
        if abs(d) >= 0.1:
            return f"  ({'+' if d > 0 else ''}{d} p.p.)"
    return ""


def montar_slack(resumo, prev_geral=None, prev_destaques=None, prev_incidentes=None):
    j = resumo["janela"]
    out = []
    out.append(f"*Monitor — Câmaras Cíveis do TJ/AL*")
    out.append(f"Julgados de *{j['inicio']}* a *{j['fim']}* (por data de julgamento)")
    out.append(f"Total: *{_milhar(resumo['total'])}* acórdãos · {len(resumo['orgaos'])} órgãos"
               + (f"  · tendência vs {resumo['tendencia_vs']}" if resumo.get("tendencia_vs") else ""))
    # Panorama geral por matéria (com tendência)
    out.append("\n*Panorama geral — por matéria*")
    linhas = [f"{'%':>5}  {'n':>5}  matéria"]
    for it in resumo["geral"]["areas"]:
        delta = _delta_pp(it["pct"], prev_geral, it["rotulo"])
        linhas.append(f"{it['pct']:>5.1f}  {_milhar(it['n']):>5}  {it['rotulo']}{delta}")
    out.append("```\n" + "\n".join(linhas) + "\n```")
    # Destaques: recortes lidos na ementa (ex.: consignado × cartão dentro do Bancário)
    for d in resumo.get("destaques", []):
        prev = (prev_destaques or {}).get(d["area"], {})
        out.append(f"\n*Destaque — dentro de {d['area']}*  ({_milhar(d['total'])} acórdãos · {d['pct_pauta']}% da pauta)")
        sl = [f"{'%':>5}  {'n':>4}  recorte (lido na ementa)"]
        for it in d["subtemas"]:
            sl.append(f"{it['pct']:>5.1f}  {it['n']:>4}  {it['rotulo']}{_delta_pp(it['pct'], prev, it['rotulo'])}")
        out.append("```\n" + "\n".join(sl) + "\n```")
    # Pulso processual: incidentes transversais (lidos na QUESTÃO EM DISCUSSÃO)
    inc = resumo.get("incidentes_proc") or []
    if inc:
        out.append("\n*Pulso processual — incidentes*  (% dos acórdãos; um pode ter vários, não soma 100%)")
        il = [f"{'%':>5}  {'n':>5}  incidente (lido na questão em discussão)"]
        for it in inc:
            il.append(f"{it['pct']:>5.1f}  {_milhar(it['n']):>5}  {it['rotulo']}{_delta_pp(it['pct'], prev_incidentes, it['rotulo'])}")
        out.append("```\n" + "\n".join(il) + "\n```")
    # Tabela completa por câmara (matéria + incidentes processuais top 8)
    for cam in resumo["orgaos"]:
        c = resumo["por_camara"][cam]
        out.append(f"\n*{cam}* — {_milhar(c['total'])} acórdãos")
        out.append("```\n" + _tabela_txt(c["areas"]) + "\n```")
        ci = c.get("incidentes") or []
        if ci:
            il = [f"{'%':>5}  {'n':>4}  incidente processual (top 8)"]
            for it in ci[:8]:
                il.append(f"{it['pct']:>5.1f}  {it['n']:>4}  {it['rotulo']}")
            out.append("```\n" + "\n".join(il) + "\n```")
    return "\n".join(out)


def _delta_md(pct, prev_map, rotulo):
    if prev_map and rotulo in prev_map:
        dd = round(pct - prev_map[rotulo], 1)
        return f"{'+' if dd > 0 else ''}{dd}" if abs(dd) >= 0.1 else "—"
    return ""


def montar_md(resumo, prev_geral=None, prev_destaques=None, prev_incidentes=None):
    j = resumo["janela"]
    md = [f"# Monitor Câmaras Cíveis TJ/AL — {resumo['rotulo']}", "",
          f"- **Janela:** {j['inicio']} a {j['fim']} (por data de julgamento)",
          f"- **Total:** {_milhar(resumo['total'])} acórdãos · {len(resumo['orgaos'])} órgãos",
          f"- **Gerado em:** {resumo['gerado_em']}"]
    if resumo.get("tendencia_vs"):
        md.append(f"- **Tendência comparada a:** {resumo['tendencia_vs']}")
    md += ["", "## Panorama geral — por matéria", "", "| % | n | matéria | Δ p.p. |", "|--:|--:|---|--:|"]
    for it in resumo["geral"]["areas"]:
        md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} | {_delta_md(it['pct'], prev_geral, it['rotulo'])} |")
    # Destaques: recortes lidos na ementa
    for d in resumo.get("destaques", []):
        prev = (prev_destaques or {}).get(d["area"], {})
        md += ["", f"## Destaque — dentro de {d['area']} ({_milhar(d['total'])} acórdãos · {d['pct_pauta']}% da pauta)",
               "", "| % | n | recorte (lido na ementa) | Δ p.p. |", "|--:|--:|---|--:|"]
        for it in d["subtemas"]:
            md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} | {_delta_md(it['pct'], prev, it['rotulo'])} |")
    # Pulso processual: incidentes transversais (geral + por câmara)
    inc = resumo.get("incidentes_proc") or []
    if inc:
        md += ["", "## Pulso processual — incidentes (transversal)",
               "", "*% dos acórdãos; um acórdão pode ter vários — não soma 100%. Lido na questão em discussão.*",
               "", "| % | n | incidente | Δ p.p. |", "|--:|--:|---|--:|"]
        for it in inc:
            md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} | {_delta_md(it['pct'], prev_incidentes, it['rotulo'])} |")
    md += ["", "## Por câmara — matéria"]
    for cam in resumo["orgaos"]:
        c = resumo["por_camara"][cam]
        md += ["", f"### {cam} — {_milhar(c['total'])} acórdãos", "", "| % | n | matéria |", "|--:|--:|---|"]
        for it in c["areas"]:
            md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} |")
        ci = c.get("incidentes") or []
        if ci:
            md += ["", "_incidentes processuais (% dos acórdãos da câmara):_",
                   "", "| % | n | incidente |", "|--:|--:|---|"]
            for it in ci[:10]:
                md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} |")
    md += ["", "## Recorte por classe processual (geral)", "", "| % | n | classe |", "|--:|--:|---|"]
    for it in resumo["geral"]["classes"]:
        md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} |")
    return "\n".join(md)


def _cmd_agregar(args):
    rows = list(csv.DictReader(open(args.inp, encoding="utf-8")))
    tax = carregar_taxonomia(getattr(args, "taxonomia", None))
    janela = {"inicio": args.inicio, "fim": args.fim, "criterio": "data_julgamento"}
    base_dir = args.base_dir or os.path.dirname(os.path.abspath(args.saida_dir))
    resumo, prev_geral, prev_destaques, prev_incidentes = agregar(
        rows, args.rotulo, janela, base_dir=base_dir, gerado_em=args.gerado_em, tax=tax)
    os.makedirs(args.saida_dir, exist_ok=True)
    json.dump(resumo, open(os.path.join(args.saida_dir, "resumo.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    open(os.path.join(args.saida_dir, "resumo.md"), "w", encoding="utf-8").write(
        montar_md(resumo, prev_geral, prev_destaques, prev_incidentes))
    open(os.path.join(args.saida_dir, "slack.txt"), "w", encoding="utf-8").write(
        montar_slack(resumo, prev_geral, prev_destaques, prev_incidentes))
    print(f"OK: resumo.json / resumo.md / slack.txt -> {args.saida_dir}", file=sys.stderr)
    print(f"   total={resumo['total']} | órgãos={len(resumo['orgaos'])}"
          + (f" | tendência vs {resumo['tendencia_vs']}" if resumo.get('tendencia_vs') else ""), file=sys.stderr)


# ----------------------------------------------------------------------------- #
# Notificar Slack (webhook) — posta a tabela por câmara no canal
# ----------------------------------------------------------------------------- #
def _cmd_notificar(args):
    # texto a postar
    if args.slack_txt:
        caminho = args.slack_txt
    else:
        caminho = os.path.join(args.saida_dir, "slack.txt")
    texto = open(caminho, encoding="utf-8").read()
    # webhook: --webhook > $SLACK_WEBHOOK_TJAL > config.local.json
    webhook = args.webhook or os.environ.get("SLACK_WEBHOOK_TJAL")
    canal = args.canal
    if not webhook and args.config and os.path.isfile(args.config):
        cfg = json.load(open(args.config, encoding="utf-8"))
        webhook = cfg.get("slack_webhook")
        canal = canal or cfg.get("slack_canal")
    if not webhook:
        print("ERRO: webhook não informado (use --webhook, $SLACK_WEBHOOK_TJAL ou --config)", file=sys.stderr)
        sys.exit(2)
    payload = json.dumps({"text": texto, "channel": canal, "unfurl_links": False}).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        print(f"Slack OK: {r.status} {r.read().decode()}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        print(f"Slack ERRO {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------------- #
# Drill-down: filtrar acórdãos de um tema/classe/câmara (para ler inteiro teor)
# ----------------------------------------------------------------------------- #
def _cmd_filtrar(args):
    rows = list(csv.DictReader(open(args.inp, encoding="utf-8")))

    def casa(r):
        ok = True
        if args.classe:
            ok = ok and args.classe.lower() in (r.get("classe_curta", "") + " " + r.get("classe", "")).lower()
        if args.area:
            ok = ok and args.area.lower() in (r.get("area", "")).lower()
        if args.tema:
            ok = ok and args.tema.lower() in (r.get("tema", "") + " " + r.get("assunto", "")).lower()
        if getattr(args, "subtema", None):
            ok = ok and args.subtema.lower() in (r.get("subtema", "")).lower()
        if getattr(args, "incidente", None):
            ok = ok and args.incidente.lower() in (r.get("incidentes", "")).lower()
        if args.camara:
            ok = ok and args.camara.lower() in (r.get("orgao", "")).lower()
        if getattr(args, "relator", None):
            ok = ok and args.relator.lower() in (r.get("relator", "")).lower()
        if args.texto:
            ok = ok and args.texto.lower() in (r.get("ementa", "")).lower()
        return ok

    sel = [r for r in rows if casa(r)]
    if args.formato == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=CAMPOS_CLASS)
        w.writeheader()
        for r in sel:
            w.writerow({k: r.get(k, "") for k in CAMPOS_CLASS})
    else:
        print(f"{len(sel)} acórdão(s):\n")
        for r in sel:
            sub = f"  · {r['subtema']}" if r.get("subtema") else ""
            print(f"- {r['numero']}  [{r.get('classe_curta', r.get('classe',''))}] "
                  f"{r.get('tema') or r.get('assunto')}{sub}")
            if r.get("incidentes"):
                print(f"    incidentes: {r['incidentes'].replace('|', ', ')}")
            print(f"    {r.get('orgao','')} · Rel. {r.get('relator','')} · julg. {r.get('data_julgamento','')}")
            print(f"    PDF: {r.get('url_pdf','')}")
            if getattr(args, "com_ementa", False):
                em = " ".join((r.get("ementa") or "").split())
                if em:
                    lim = getattr(args, "max_ementa", 1200)
                    print(f"    ementa: {em[:lim]}{'…' if len(em) > lim else ''}")
    print(f"\n({len(sel)} de {len(rows)})", file=sys.stderr)


def _cmd_publicar(args):
    """Copia o registro datado p/ o clone local do repo (registros/<rotulo>/) e,
    com --push, commita e envia. O CSV do cível já é enxuto (ementa inline, sem
    coluna de inteiro teor), então vai copiado como está."""
    import shutil
    import subprocess
    destino = os.path.join(args.repo_dir, "registros", args.rotulo)
    os.makedirs(destino, exist_ok=True)
    copiados = []
    for nome in ("classificado.csv", "resumo.json", "resumo.md", "slack.txt"):
        src = os.path.join(args.saida_dir, nome)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(destino, nome))
            copiados.append(nome)
    print(f"OK: {len(copiados)} arquivo(s) -> {destino} ({', '.join(copiados)})", file=sys.stderr)
    if not args.push:
        return

    def git(*a):
        return subprocess.run(["git", "-C", args.repo_dir, *a], capture_output=True, text=True)
    git("add", os.path.join("registros", args.rotulo))
    r = git("commit", "-m", f"registros: rodada {args.rotulo}")
    saida = r.stdout + r.stderr
    if r.returncode != 0 and "nothing to commit" in saida:
        print("git: nada a commitar (registro já publicado)", file=sys.stderr)
        return
    if r.returncode != 0:
        print(f"git commit falhou: {saida.strip()}", file=sys.stderr)
        return
    rp = git("push")
    if rp.returncode != 0:
        print(f"git push falhou: {(rp.stdout + rp.stderr).strip()}", file=sys.stderr)
    else:
        print(f"git: publicado e enviado ({args.rotulo})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Monitor Câmaras Cíveis TJ/AL")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("coletar", help="Busca acórdãos no cjsg e grava CSV bruto")
    c.add_argument("--dias", type=int, help="Janela móvel: últimos N dias (por data de julgamento)")
    c.add_argument("--inicio", help="Data início dd/mm/aaaa (com --fim)")
    c.add_argument("--fim", help="Data fim dd/mm/aaaa (com --inicio)")
    c.add_argument("--orgaos", help="Lista de códigos separados por vírgula (sobrescreve o default)")
    c.add_argument("--exec-fiscal", action="store_true", help="Incluir câmaras de execução fiscal")
    c.add_argument("--pausa", type=float, default=0.5, help="Pausa entre páginas (s)")
    c.add_argument("--out", required=True, help="CSV de saída")
    c.set_defaults(func=_cmd_coletar)

    k = sub.add_parser("classificar", help="Classifica área/tema/classe (determinístico)")
    k.add_argument("--inp", required=True, help="CSV bruto da coleta")
    k.add_argument("--out", required=True, help="CSV classificado de saída")
    k.add_argument("--taxonomia", help="Caminho do taxonomia.json (default: ao lado do script)")
    k.set_defaults(func=_cmd_classificar)

    a = sub.add_parser("agregar", help="Percentuais por câmara + tendência + registro + msg Slack")
    a.add_argument("--inp", required=True, help="CSV classificado (com resíduo já preenchido)")
    a.add_argument("--saida-dir", required=True, help="Pasta datada AAAA-MM-DD onde gravar resumo/slack")
    a.add_argument("--base-dir", help="Pasta-mãe p/ comparar tendência (default: pai de --saida-dir)")
    a.add_argument("--rotulo", required=True, help="Rótulo da rodada (AAAA-MM-DD)")
    a.add_argument("--inicio", required=True, help="Janela início dd/mm/aaaa")
    a.add_argument("--fim", required=True, help="Janela fim dd/mm/aaaa")
    a.add_argument("--gerado-em", help="Carimbo de geração (default: agora)")
    a.add_argument("--taxonomia", help="Caminho do taxonomia.json (p/ os destaques por subtema)")
    a.set_defaults(func=_cmd_agregar)

    f = sub.add_parser("filtrar", help="Drill-down: lista acórdãos de um tema/classe/câmara")
    f.add_argument("--inp", required=True, help="CSV classificado (da pasta datada)")
    f.add_argument("--classe", help="Filtra por classe (substring, ex.: 'Mandado de Segurança')")
    f.add_argument("--area", help="Filtra por área/matéria (substring, ex.: 'Sucessões')")
    f.add_argument("--tema", help="Filtra por tema/assunto (substring, ex.: 'testamento')")
    f.add_argument("--subtema", help="Filtra por subtema lido na ementa (substring, ex.: 'Consignado')")
    f.add_argument("--incidente", help="Filtra por incidente processual (substring, ex.: 'Tutela', 'Honorários')")
    f.add_argument("--camara", help="Filtra por câmara (substring, ex.: '3ª')")
    f.add_argument("--texto", help="Filtra por termo na ementa (substring)")
    f.add_argument("--relator", help="Filtra por relator(a) (substring)")
    f.add_argument("--com-ementa", action="store_true",
                   help="Inclui o texto da ementa na saída (use só quando pedirem ementas)")
    f.add_argument("--max-ementa", type=int, default=1200, help="Corta a ementa em N caracteres (default 1200)")
    f.add_argument("--formato", choices=["lista", "csv"], default="lista")
    f.set_defaults(func=_cmd_filtrar)

    p = sub.add_parser("publicar", help="Copia o registro datado p/ o clone do repo (registros/<rotulo>/) e, com --push, commita e envia")
    p.add_argument("--saida-dir", required=True, help="Pasta datada de origem (classificado.csv, resumo.json/md, slack.txt)")
    p.add_argument("--rotulo", required=True, help="Rótulo da rodada (AAAA-MM-DD)")
    p.add_argument("--repo-dir", required=True, help="Clone local do repo público")
    p.add_argument("--push", action="store_true", help="git add/commit/push do registro")
    p.set_defaults(func=_cmd_publicar)

    n = sub.add_parser("notificar", help="Posta a tabela (slack.txt) no canal via webhook")
    n.add_argument("--saida-dir", help="Pasta datada (lê slack.txt dela)")
    n.add_argument("--slack-txt", help="Caminho direto do slack.txt (alternativa a --saida-dir)")
    n.add_argument("--webhook", help="URL do Incoming Webhook (ou use $SLACK_WEBHOOK_TJAL / --config)")
    n.add_argument("--config", help="config.local.json (lê slack_webhook/slack_canal)")
    n.add_argument("--canal", default="#tjal-camaras-civeis", help="Canal de destino")
    n.set_defaults(func=_cmd_notificar)

    args = ap.parse_args()
    # validação dias x inicio/fim
    if args.cmd == "coletar" and not args.dias and not (args.inicio and args.fim):
        ap.error("use --dias OU (--inicio e --fim)")
    args.func(args)


if __name__ == "__main__":
    main()
