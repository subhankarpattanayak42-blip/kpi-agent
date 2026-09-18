"""
PPT generator — matches Jul31 Thomas Pfister Role Transformation format.
One slide per role with a 10-column KPI table (category | KPI | dot+narrative x4 regions).
"""
import re
import os
from datetime import datetime

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml import parse_xml
from lxml import etree

# ── Colours ───────────────────────────────────────────────────────────────────
C_BLUE    = RGBColor(0x19, 0x90, 0xFE)   # title #1990FE
C_GREEN   = RGBColor(0x92, 0xD0, 0x50)   # at/above target
C_AMBER   = RGBColor(0xFF, 0xC0, 0x00)   # 70-99%
C_RED     = RGBColor(0xFF, 0x00, 0x00)   # below 70%
C_GREY    = RGBColor(0xBF, 0xBF, 0xBF)   # no data
C_BLACK   = RGBColor(0x00, 0x00, 0x00)
C_DARK    = RGBColor(0x26, 0x26, 0x26)
C_HDR_BG  = RGBColor(0xF2, 0xF2, 0xF2)  # header row fill
C_NOTE    = RGBColor(0x59, 0x59, 0x59)

ROLE_NAMES = {
    "EA":  "Enterprise Architect",
    "DA":  "Digital Advisor",
    "CEP": "Customer Engagement Partner",
    "SPM": "Solution Portfolio Manager",
}

REGIONS = ["APAC", "EMEA", "MEE", "AMER"]

# ── Status derivation ─────────────────────────────────────────────────────────

def _status_color(value, narrative):
    """Return RGBColor for status dot based on value string and narrative."""
    if not value or value in ("—", "-", ""):
        return C_GREY
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', str(value))
    if m:
        pct = float(m.group(1))
        if pct >= 90:
            return C_GREEN
        elif pct >= 70:
            return C_AMBER
        else:
            return C_RED
    # non-% value — read narrative sentiment
    nar = (narrative or "").lower()
    if any(w in nar for w in ("on track", "exceeded", "above", "ahead", "strong", "100%", "complete")):
        return C_GREEN
    if any(w in nar for w in ("below", "behind", "risk", "challenge", "not yet", "0%", "struggling")):
        return C_RED
    return C_AMBER

def _overall_status(submissions, kpis):
    """Derive overall role status from all region/KPI dots."""
    colors = []
    for region in REGIONS:
        sub = submissions.get(region, {})
        kpi_data = sub.get("kpi_data", {}) if sub else {}
        for kpi in kpis:
            entry = kpi_data.get(kpi["id"], {}) if kpi_data else {}
            c = _status_color(entry.get("value", ""), entry.get("narrative", ""))
            colors.append(c)
    if not colors:
        return C_GREY
    reds   = sum(1 for c in colors if c == C_RED)
    greens = sum(1 for c in colors if c == C_GREEN)
    if reds > len(colors) * 0.3:
        return C_RED
    if greens > len(colors) * 0.6:
        return C_GREEN
    return C_AMBER

# ── python-pptx helpers ───────────────────────────────────────────────────────

def _add_textbox(slide, left, top, width, height, text, size, bold=False, color=None, italic=False, align=PP_ALIGN.LEFT):
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = txBox.text_frame
    tf.word_wrap = True
    para = tf.paragraphs[0]
    para.alignment = align
    run = para.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color:
        run.font.color.rgb = color
    return txBox

def _set_cell_fill(cell, rgb):
    """Set solid fill on a table cell."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    # remove existing fill
    for child in list(tcPr):
        if child.tag.endswith('}solidFill') or child.tag.endswith('}noFill') or child.tag.endswith('}gradFill'):
            tcPr.remove(child)
    solidFill = parse_xml(
        '<a:solidFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:srgbClr val="{:02X}{:02X}{:02X}"/>'.format(rgb[0], rgb[1], rgb[2]) +
        '</a:solidFill>'
    )
    tcPr.insert(0, solidFill)

def _cell_text(cell, text, size, bold=False, color=None, italic=False, align=PP_ALIGN.LEFT):
    tf = cell.text_frame
    tf.word_wrap = True
    para = tf.paragraphs[0]
    para.alignment = align
    # clear existing runs
    for run in list(para.runs):
        para._p.remove(run._r)
    if not text:
        return
    run = para.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color:
        run.font.color.rgb = color

def _set_cell_margins(cell, top=0.02, bottom=0.02, left=0.05, right=0.05):
    """Set cell internal margins in inches."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    # marT/marB/marL/marR in EMUs
    tcPr.set('marT', str(int(top * 914400)))
    tcPr.set('marB', str(int(bottom * 914400)))
    tcPr.set('marL', str(int(left * 914400)))
    tcPr.set('marR', str(int(right * 914400)))

def _set_cell_border(cell, color_hex="D9D9D9", width_pt=0.5):
    """Apply thin border on all sides of a cell."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    w_emu = int(width_pt * 12700)
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    for side in ("lnL", "lnR", "lnT", "lnB"):
        existing = tcPr.find(qn("a:" + side))
        if existing is not None:
            tcPr.remove(existing)
        ln = etree.SubElement(tcPr, qn("a:" + side))
        ln.set("w", str(w_emu))
        ln.set("cap", "flat")
        ln.set("cmpd", "sng")
        solidFill = etree.SubElement(ln, qn("a:solidFill"))
        srgbClr = etree.SubElement(solidFill, qn("a:srgbClr"))
        srgbClr.set("val", color_hex)

def _rotate_cell_text_vertical(cell):
    """Set text direction to vertical (bottom-to-top) in a cell."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcPr.set('vert', 'vert270')

# ── Main generator ────────────────────────────────────────────────────────────

def generate_role_ppt(role, sponsor_name, month_label, submissions, kpis, output_path):
    """
    Generate a single-slide PPT in the Thomas Pfister format for one role.
    role: "EA", "DA", "CEP"
    sponsor_name: e.g. "Thomas Pfister"
    month_label: e.g. "September 2026"
    submissions: dict keyed by region, each with kpi_data dict and status
    kpis: list of dicts {id, label, target, category}
    output_path: .pptx output path
    Returns output_path.
    """
    prs = Presentation()
    prs.slide_width  = Inches(13.34)
    prs.slide_height = Inches(7.50)

    blank_layout = prs.slide_layouts[6]  # completely blank
    slide = prs.slides.add_slide(blank_layout)

    role_full = ROLE_NAMES.get(role, role)
    today_str = datetime.now().strftime("%d/%m/%Y")

    # ── Title ─────────────────────────────────────────────────────────────────
    _add_textbox(
        slide,
        left=0.55, top=0.50, width=12.23, height=0.35,
        text="Role Transformation – {} | Global Role Sponsor – Status Update to Global Leadership".format(role_full),
        size=18, bold=False, color=C_BLUE
    )

    # ── Last Updated box ──────────────────────────────────────────────────────
    _add_textbox(
        slide,
        left=11.40, top=0.26, width=1.68, height=0.24,
        text="Last Updated: {}".format(today_str),
        size=6, bold=False, color=C_BLACK,
        align=PP_ALIGN.RIGHT
    )

    # ── Sponsor info bar (1 row × 4 cols) ────────────────────────────────────
    overall_color = _overall_status(submissions, kpis)
    bar_table = slide.shapes.add_table(
        1, 4,
        Inches(0.54), Inches(0.93),
        Inches(9.5), Inches(0.27)
    ).table
    # col widths
    bar_table.columns[0].width = Inches(1.0)
    bar_table.columns[1].width = Inches(2.5)
    bar_table.columns[2].width = Inches(5.7)
    bar_table.columns[3].width = Inches(0.3)

    _cell_text(bar_table.cell(0, 0), "Role Sponsor", 10, bold=True, color=C_DARK)
    _cell_text(bar_table.cell(0, 1), sponsor_name, 10, bold=False, color=C_BLACK)
    _cell_text(bar_table.cell(0, 2), "Global Role Transformation Execution Status", 10, bold=True, color=C_DARK)
    _cell_text(bar_table.cell(0, 3), "⬤", 8, bold=False, color=overall_color, align=PP_ALIGN.CENTER)

    for c in range(4):
        _set_cell_margins(bar_table.cell(0, c), 0.01, 0.01, 0.05, 0.05)
        _set_cell_border(bar_table.cell(0, c), "D0D0D0", 0.5)

    # ── Build KPI row data ────────────────────────────────────────────────────
    # Group KPIs by category preserving insertion order
    from collections import OrderedDict
    cat_kpis = OrderedDict()
    for kpi in kpis:
        cat = kpi.get("category", "General")
        cat_kpis.setdefault(cat, []).append(kpi)

    # Flatten to (category_label_or_None, kpi) rows — category only on first row of group
    rows = []   # list of (show_cat_label, category, kpi)
    for cat, cat_kpi_list in cat_kpis.items():
        for i, kpi in enumerate(cat_kpi_list):
            rows.append((i == 0, cat, kpi))

    n_data_rows = len(rows)
    total_rows = 1 + n_data_rows + 1  # header + data + observations

    # ── Main KPI table ────────────────────────────────────────────────────────
    tbl_top = 1.24
    tbl_height = max(5.6, n_data_rows * 0.55 + 0.5 + 0.22)
    tbl_height = min(tbl_height, 5.9)  # cap to slide

    tbl_shape = slide.shapes.add_table(
        total_rows, 10,
        Inches(0.55), Inches(tbl_top),
        Inches(12.25), Inches(tbl_height)
    )
    tbl = tbl_shape.table

    # Column widths
    col_widths = [0.9, 1.7, 0.22, 1.65, 0.22, 1.65, 0.22, 1.65, 0.22, 1.65]
    for ci, w in enumerate(col_widths):
        tbl.columns[ci].width = Inches(w)

    # Row heights
    tbl.rows[0].height = Inches(0.22)
    for ri in range(1, total_rows - 1):
        row_h = tbl_height - 0.22 - 0.45
        row_h = row_h / n_data_rows if n_data_rows else 0.5
        tbl.rows[ri].height = Inches(max(0.38, row_h))
    tbl.rows[total_rows - 1].height = Inches(0.45)

    # ── Header row ────────────────────────────────────────────────────────────
    hdr_labels = ["", "KPI", "APAC", "", "EMEA", "", "MEE", "", "AMER", ""]
    for ci, lbl in enumerate(hdr_labels):
        cell = tbl.cell(0, ci)
        _set_cell_fill(cell, C_HDR_BG)
        _cell_text(cell, lbl, 9, bold=True, color=C_DARK, align=PP_ALIGN.CENTER)
        _set_cell_margins(cell, 0.01, 0.01, 0.04, 0.04)
        _set_cell_border(cell, "C0C0C0", 0.5)

    # ── Data rows ─────────────────────────────────────────────────────────────
    # Track category row spans for merging later
    cat_start_row = {}   # cat -> first data row index (1-based in table)
    cat_end_row   = {}

    for row_i, (show_cat, cat, kpi) in enumerate(rows):
        ri = row_i + 1  # table row index

        if show_cat:
            cat_start_row[cat] = ri
        cat_end_row[cat] = ri

        # col0: category (will be merged + vertical after loop)
        cell_cat = tbl.cell(ri, 0)
        if show_cat:
            _cell_text(cell_cat, cat, 9, bold=True, color=C_DARK, align=PP_ALIGN.CENTER)
        _set_cell_margins(cell_cat, 0.01, 0.01, 0.02, 0.02)
        _set_cell_border(cell_cat, "C0C0C0", 0.5)

        # col1: KPI label
        cell_kpi = tbl.cell(ri, 1)
        _cell_text(cell_kpi, kpi.get("label", ""), 9, bold=True, color=C_DARK)
        _set_cell_margins(cell_kpi, 0.02, 0.02, 0.05, 0.03)
        _set_cell_border(cell_kpi, "C0C0C0", 0.5)

        # cols 2-9: dot + narrative per region
        for reg_i, region in enumerate(REGIONS):
            dot_col = 2 + reg_i * 2
            nar_col = 3 + reg_i * 2
            sub = submissions.get(region, {})
            kpi_data = {}
            if sub:
                raw = sub.get("kpi_data", {})
                if isinstance(raw, str):
                    import json
                    try:
                        raw = json.loads(raw)
                    except Exception:
                        raw = {}
                kpi_data = raw or {}

            entry = kpi_data.get(kpi["id"], {}) if kpi_data else {}
            value     = entry.get("value", "") if entry else ""
            narrative = entry.get("narrative", "") if entry else ""

            if not sub:
                dot_color = C_GREY
                nar_text  = "Not submitted"
            else:
                dot_color = _status_color(value, narrative)
                nar_text  = narrative or value or "—"

            # Dot cell
            cell_dot = tbl.cell(ri, dot_col)
            _cell_text(cell_dot, "⬤", 7, bold=False, color=dot_color, align=PP_ALIGN.CENTER)
            _set_cell_margins(cell_dot, 0.01, 0.01, 0.01, 0.01)
            _set_cell_border(cell_dot, "C0C0C0", 0.5)

            # Narrative cell
            cell_nar = tbl.cell(ri, nar_col)
            _cell_text(cell_nar, nar_text, 6, bold=False, color=C_BLACK)
            cell_nar.text_frame.word_wrap = True
            _set_cell_margins(cell_nar, 0.02, 0.02, 0.04, 0.04)
            _set_cell_border(cell_nar, "C0C0C0", 0.5)

    # ── Merge category cells vertically ──────────────────────────────────────
    for cat, start in cat_start_row.items():
        end = cat_end_row[cat]
        if end > start:
            tbl.cell(start, 0).merge(tbl.cell(end, 0))
        _rotate_cell_text_vertical(tbl.cell(start, 0))

    # ── Observations row (last row, cols 0-1 label, cols 2-9 merged text) ────
    obs_ri = total_rows - 1
    cell_obs_lbl = tbl.cell(obs_ri, 0)
    _cell_text(cell_obs_lbl, "Global Role Sponsor\nObservations", 7, bold=True, color=C_DARK)
    _set_cell_margins(cell_obs_lbl, 0.02, 0.02, 0.04, 0.04)
    _set_cell_border(cell_obs_lbl, "C0C0C0", 0.5)

    # merge col0+col1 for label
    tbl.cell(obs_ri, 0).merge(tbl.cell(obs_ri, 1))

    # merge cols 2-9 for text
    tbl.cell(obs_ri, 2).merge(tbl.cell(obs_ri, 9))
    cell_obs_text = tbl.cell(obs_ri, 2)
    _cell_text(cell_obs_text, "", 6, bold=False, italic=True, color=C_NOTE)
    _set_cell_margins(cell_obs_text, 0.02, 0.02, 0.05, 0.05)
    _set_cell_border(cell_obs_text, "C0C0C0", 0.5)

    # ── Footer note ───────────────────────────────────────────────────────────
    _add_textbox(
        slide,
        left=3.5, top=7.26, width=4.71, height=0.24,
        text="Note: Status indicators reflect progress of role transformation execution against targets set for FY2026.",
        size=7, bold=False, color=C_NOTE
    )

    # ── Legend ────────────────────────────────────────────────────────────────
    legend_box = slide.shapes.add_textbox(
        Inches(8.15), Inches(7.10), Inches(5.08), Inches(0.25)
    )
    tf = legend_box.text_frame
    para = tf.paragraphs[0]
    para.alignment = PP_ALIGN.RIGHT

    def _add_legend_run(para, text, color, size=6):
        run = para.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.color.rgb = color

    _add_legend_run(para, "⬤ At or above target  ", C_GREEN)
    _add_legend_run(para, "⬤ Within 70%-99% of target  ", C_AMBER)
    _add_legend_run(para, "⬤ Below 70% of target", C_RED)

    prs.save(output_path)
    return output_path
