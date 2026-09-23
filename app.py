import io
import math
import os
import time
from typing import List, Optional
from google import genai
from google.genai import types
from google.genai.errors import ServerError
from pydantic import BaseModel, Field
from fpdf import FPDF
import streamlit as st
import gspread
from datetime import datetime

# -------------------------------------------------------------
# 1. PAGE SETUP
# -------------------------------------------------------------
st.set_page_config(
    page_title="Contractor Remodel Estimator",
    page_icon="🔨",
    layout="wide",
)

API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
if API_KEY == "YOUR_GEMINI_API_KEY_HERE":
    st.warning(
        "⚠️ No Gemini API key found. Set the `GEMINI_API_KEY` environment variable "
        "to enable AI scope extraction."
    )


def get_client():
    """Create the Gemini client lazily so a missing key fails with a clear message."""
    key = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
    if key == "YOUR_GEMINI_API_KEY_HERE":
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=key)


# -------------------------------------------------------------
# 2. EDITABLE UNIT PRICES
# -------------------------------------------------------------
# (key: (friendly label, default price))
DEFAULT_PRICES = {
    # Deck
    "deck_joist": ("2x6xL' Treated Framing Joist (per pc)", 9.50),
    "deck_board": ("5/4x6xW' Treated Deck Board (per pc)", 11.25),
    "deck_post": ("4x4x8' Treated Support Post (per pc)", 14.00),
    "deck_screws": ("3-inch Deck Screws, 5 lb box", 32.00),
    "deck_handrail": ("2x4x8' Handrail Lumber (per pc)", 6.75),
    "deck_baluster": ('2x2x36" Wood Baluster (per pc)', 1.80),
    "deck_ramp_kit": ("Ramp Stringers & Decking Kit", 165.00),
    # Bathroom
    "bath_backer": ('1/2" x 3\'x5\' Cement Backer Board (per sheet)', 15.50),
    "bath_membrane": ("Waterproofing Membrane, 1 gal", 58.00),
    "bath_wall_tile": ("Wall Tile 12x24 Porcelain (per sq ft)", 4.50),
    "bath_floor_tile": ("Floor Tile Mosaic (per sq ft)", 6.20),
    "bath_valve_kit": ("Shower Valve & Trim Kit", 185.00),
    # Kitchen
    "kit_base_cab": ('Standard Base Cabinet avg 30" (per unit)', 240.00),
    "kit_wall_cab": ('Standard Wall Cabinet 30" H (per unit)', 195.00),
    "kit_countertop": ("Quartz Countertop (per sq ft)", 55.00),
    "kit_backsplash": ("Subway Tile Backsplash (per sq ft)", 4.75),
    "kit_sink_kit": ("Sink & Pull-down Faucet Kit", 260.00),
    # Fence
    "fence_picket": ("6' Cedar Picket (per pc)", 3.25),
    "fence_post": ("4x4x8' Treated Fence Post (per pc)", 14.00),
    "fence_rail": ("2x4x8' Fence Rail (per pc)", 6.75),
    "fence_gate_kit": ("Walk Gate Hardware Kit", 89.00),
    # Flooring
    "floor_lvp": ("Luxury Vinyl Plank (per sq ft)", 3.80),
    "floor_underlay": ("Underlayment (per sq ft)", 0.45),
    "floor_trim_lf": ("Quarter-round Trim (per linear ft)", 1.90),
    # Painting
    "paint_gal": ("Interior Paint (per gallon)", 42.00),
    "paint_supplies_kit": ("Rollers, Tape & Supplies Kit", 35.00),
}

if "prices" not in st.session_state:
    st.session_state.prices = {k: v[1] for k, v in DEFAULT_PRICES.items()}

with st.sidebar:
    st.header("⚙️ Pricing Settings")
    markup_percent = st.slider("Contractor Markup (%)", min_value=0, max_value=50, value=20, step=5)
    tax_percent = st.slider("Estimated Sales Tax (%)", min_value=0.0, max_value=12.0, value=7.0, step=0.5)
    st.divider()
    with st.expander("💲 Edit Unit Prices"):
        st.caption("Prices update the estimate immediately.")
        for key, (label, _default) in DEFAULT_PRICES.items():
            st.session_state.prices[key] = st.number_input(
                label, min_value=0.0, value=float(st.session_state.prices[key]),
                step=0.5, format="%.2f", key=f"price_{key}",
            )
        if st.button("Reset prices to defaults"):
            st.session_state.prices = {k: v[1] for k, v in DEFAULT_PRICES.items()}
            st.rerun()
    st.divider()
    st.info("💡 **Quick Examples:**\n\n- *5x5 deck with treated wood, handrails, and a ramp*\n- *8x10 bathroom remodel*\n- *Kitchen remodel with 20ft of cabinets*\n- *100ft cedar fence with a gate*\n- *12x15 room new flooring*\n- *Paint a 12x15 room*")


# -------------------------------------------------------------
# 3. DATA SCHEMAS
# -------------------------------------------------------------
class SingleProject(BaseModel):
    trade: str = Field(
        description="The type of job: 'deck', 'bathroom', 'kitchen', "
                    "'fence', 'flooring', or 'painting'."
    )
    length_ft: Optional[float] = Field(default=0.0, description="Length in feet")
    width_ft: Optional[float] = Field(default=0.0, description="Width in feet")
    linear_ft: Optional[float] = Field(
        default=0.0, description="Linear feet of cabinets, railings, fence, trim, etc."
    )
    has_handrails: Optional[bool] = Field(default=False)
    has_ramp: Optional[bool] = Field(default=False)


class ContractorPromptExtraction(BaseModel):
    projects: List[SingleProject]


# -------------------------------------------------------------
# 4. TAKEOFF CALCULATIONS (prices injected, never hardcoded)
# -------------------------------------------------------------
def calculate_deck(proj: SingleProject, p):
    l = proj.length_ft or 10
    w = proj.width_ft or 10
    waste = 1.10

    joists_count = math.ceil(((w * 12) / 16) + 1)
    deck_boards = math.ceil(((l * 12) / 5.5) * waste)

    items = [
        {"name": f"2x6x{int(l)}' Treated Framing Joists", "qty": joists_count, "unit": "pcs", "unit_price": p["deck_joist"]},
        {"name": f"5/4x6x{int(w)}' Treated Deck Boards", "qty": deck_boards, "unit": "pcs", "unit_price": p["deck_board"]},
        {"name": "4x4x8' Treated Support Posts", "qty": 4, "unit": "pcs", "unit_price": p["deck_post"]},
        {"name": "3-inch Exterior Deck Screws (5 lb box)", "qty": 1, "unit": "box", "unit_price": p["deck_screws"]},
    ]
    if proj.has_handrails:
        items.append({"name": "2x4x8' Handrail Lumber", "qty": 6, "unit": "pcs", "unit_price": p["deck_handrail"]})
        items.append({"name": '2x2x36" Wood Balusters', "qty": 35, "unit": "pcs", "unit_price": p["deck_baluster"]})
    if proj.has_ramp:
        items.append({"name": "Ramp Stringers & Decking Kit", "qty": 1, "unit": "kit", "unit_price": p["deck_ramp_kit"]})
    return items


def calculate_bathroom(proj: SingleProject, p):
    l = proj.length_ft or 8
    w = proj.width_ft or 5
    floor_sqft = l * w
    wet_wall_sqft = 15 * 8

    return [
        {"name": '1/2" x 3\'x5\' Cement Backer Boards', "qty": math.ceil((wet_wall_sqft / 15) * 1.1), "unit": "sheets", "unit_price": p["bath_backer"]},
        {"name": "Waterproofing Membrane (1 Gal)", "qty": 1, "unit": "bucket", "unit_price": p["bath_membrane"]},
        {"name": "Wall Tile (12x24 Porcelain)", "qty": round(wet_wall_sqft * 1.15), "unit": "sq ft", "unit_price": p["bath_wall_tile"]},
        {"name": "Floor Tile (Mosaic)", "qty": round(floor_sqft * 1.15), "unit": "sq ft", "unit_price": p["bath_floor_tile"]},
        {"name": "Shower Valve & Trim Kit", "qty": 1, "unit": "kit", "unit_price": p["bath_valve_kit"]},
    ]


def calculate_kitchen(proj: SingleProject, p):
    cabinets_lf = proj.linear_ft or 15
    countertop_sqft = cabinets_lf * 2.1

    return [
        {"name": 'Standard Base Cabinets (avg 30")', "qty": math.ceil(cabinets_lf / 2.5), "unit": "units", "unit_price": p["kit_base_cab"]},
        {"name": 'Standard Wall Cabinets (30" H)', "qty": math.ceil(cabinets_lf / 2.5), "unit": "units", "unit_price": p["kit_wall_cab"]},
        {"name": "Quartz Countertop Slab", "qty": round(countertop_sqft * 1.1), "unit": "sq ft", "unit_price": p["kit_countertop"]},
        {"name": "Subway Tile Backsplash", "qty": round(cabinets_lf * 1.5 * 1.15), "unit": "sq ft", "unit_price": p["kit_backsplash"]},
        {"name": "Sink & Pull-down Faucet Kit", "qty": 1, "unit": "kit", "unit_price": p["kit_sink_kit"]},
    ]


def calculate_fence(proj: SingleProject, p):
    lf = proj.linear_ft or 100
    pickets = math.ceil(lf * 2.2)          # ~5.5" pickets + waste
    posts = math.ceil(lf / 8) + 1         # post every 8 ft
    rails = math.ceil(lf / 8) * 2         # 2 rails per section

    return [
        {"name": "6' Cedar Pickets", "qty": pickets, "unit": "pcs", "unit_price": p["fence_picket"]},
        {"name": "4x4x8' Treated Fence Posts", "qty": posts, "unit": "pcs", "unit_price": p["fence_post"]},
        {"name": "2x4x8' Fence Rails", "qty": rails, "unit": "pcs", "unit_price": p["fence_rail"]},
        {"name": "Walk Gate Hardware Kit", "qty": 1, "unit": "kit", "unit_price": p["fence_gate_kit"]},
    ]


def calculate_flooring(proj: SingleProject, p):
    l = proj.length_ft or 12
    w = proj.width_ft or 12
    sqft = l * w
    perimeter = 2 * (l + w)

    return [
        {"name": "Luxury Vinyl Plank", "qty": round(sqft * 1.1), "unit": "sq ft", "unit_price": p["floor_lvp"]},
        {"name": "Underlayment", "qty": round(sqft * 1.05), "unit": "sq ft", "unit_price": p["floor_underlay"]},
        {"name": "Quarter-round Trim", "qty": math.ceil(perimeter * 1.05), "unit": "lin ft", "unit_price": p["floor_trim_lf"]},
    ]


def calculate_painting(proj: SingleProject, p):
    l = proj.length_ft or 12
    w = proj.width_ft or 12
    wall_sqft = 2 * (l + w) * 8            # 8 ft ceilings
    gallons = math.ceil(wall_sqft / 350)  # ~350 sq ft per gallon

    return [
        {"name": "Interior Paint", "qty": gallons, "unit": "gal", "unit_price": p["paint_gal"]},
        {"name": "Rollers, Tape & Supplies Kit", "qty": 1, "unit": "kit", "unit_price": p["paint_supplies_kit"]},
    ]


CALCULATORS = {
    "deck": calculate_deck,
    "bath": calculate_bathroom,
    "kitchen": calculate_kitchen,
    "fence": calculate_fence,
    "floor": calculate_flooring,
    "paint": calculate_painting,
}


# -------------------------------------------------------------
# 5. LLM CALL WITH RETRY LOGIC (Handles 503 errors gracefully)
# -------------------------------------------------------------
def parse_with_ai(user_prompt: str, max_retries: int = 3):
    client = get_client()
    for attempt in range(1, max_retries + 1):
        try:
            chat = client.chats.create(model="gemini-3.6-flash")
            response = chat.send_message(
                f"Extract the remodel projects and dimensions from this description: {user_prompt} ",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ContractorPromptExtraction,
                    temperature=0.0,
                ),
            )
            return ContractorPromptExtraction.model_validate_json(response.text)
        except ServerError:
            if attempt < max_retries:
                time.sleep(2)
            else:
                raise


# -------------------------------------------------------------
# 6. PDF INVOICE
# -------------------------------------------------------------
def build_invoice_pdf(estimate) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "Estimate & Proposal", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Date: {estimate['date']}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Scope: {', '.join(estimate['project_tags'])}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 10)
    col_w = [90, 22, 28, 28, 28]
    for h, w in zip(["Item", "Qty", "Unit $", "Cost $", "Client $"], col_w):
        pdf.cell(w, 8, h, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for m in estimate["materials"]:
        pdf.cell(col_w[0], 7, m["name"][:52], border=1)
        pdf.cell(col_w[1], 7, f"{m['qty']} {m['unit']}", border=1)
        pdf.cell(col_w[2], 7, f"${m['unit_price']:.2f}", border=1)
        pdf.cell(col_w[3], 7, f"${m['qty'] * m['unit_price']:.2f}", border=1)
        pdf.cell(col_w[4], 7, f"${m['qty'] * m['unit_price'] * (1 + estimate['markup_percent'] / 100):.2f}", border=1)
        pdf.ln()
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Materials Total: ${estimate['subtotal'] + estimate['markup_amount']:,.2f}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Estimated Taxes ({estimate['tax_percent']}%): ${estimate['tax_amount']:,.2f}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, f"Grand Total: ${estimate['grand_total']:,.2f}", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# -------------------------------------------------------------
# 7. GOOGLE SHEETS
# -------------------------------------------------------------
def get_gspread_client():
    """Streamlit Cloud secrets first, local secrets.json as fallback."""
    try:
        creds = dict(st.secrets["gcp_service_account"])
        return gspread.service_account_from_dict(creds)
    except Exception:
        return gspread.service_account(filename="secrets.json")


# -------------------------------------------------------------
# 8. UI LAYOUT
# -------------------------------------------------------------
st.title("🔨 Contractor Material Estimator & Invoice Generator")
st.caption("Enter freeform job specs to extract dimensions, run material takeoffs, and generate invoices.")

if "estimate" not in st.session_state:
    st.session_state.estimate = None

# Prompt Input Box
default_text = "a deck with treated wood the deck would be 5x5 with hand rails and a ramp"
user_prompt = st.text_area("Describe the Job Scope:", value=default_text, height=90)

if st.button("Generate Estimate & Invoice", type="primary", use_container_width=True):
    if not user_prompt.strip():
        st.warning("Please enter a job description.")
    else:
        with st.spinner("Analyzing project scope & calculating material quantities..."):
            try:
                data = parse_with_ai(user_prompt)
            except Exception as e:
                st.error(f"Could not reach the Gemini API: {e}")
                st.stop()

        all_materials = []
        project_tags = []
        prices = st.session_state.prices

        for proj in data.projects:
            trade_clean = proj.trade.lower().strip()
            calc = next((fn for key, fn in CALCULATORS.items() if key in trade_clean), None)
            if calc is None:
                continue
            project_tags.append(trade_clean.upper())
            for itm in calc(proj, prices):
                itm["trade"] = trade_clean.upper()
                all_materials.append(itm)

        if not all_materials:
            st.error("No recognized projects (Deck, Bath, Kitchen, Fence, Flooring, Painting) found. "
                     "Try specifying dimensions or room types.")
        else:
            cost_subtotal = sum(m["qty"] * m["unit_price"] for m in all_materials)
            markup_amount = cost_subtotal * (markup_percent / 100.0)
            tax_amount = (cost_subtotal + markup_amount) * (tax_percent / 100.0)
            grand_total = cost_subtotal + markup_amount + tax_amount

            # Persist so the estimate survives reruns (buttons below stay visible)
            st.session_state.estimate = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "materials": all_materials,
                "project_tags": project_tags,
                "subtotal": cost_subtotal,
                "markup_amount": markup_amount,
                "markup_percent": markup_percent,
                "tax_amount": tax_amount,
                "tax_percent": tax_percent,
                "grand_total": grand_total,
            }

# Render the persisted estimate (survives Streamlit reruns)
est = st.session_state.estimate
if est:
    st.success(f"Recognized Scope: **{', '.join(est['project_tags'])}**")

    col1, col2, col3 = st.columns(3)
    col1.metric("Wholesale Material Cost", f"${est['subtotal']:,.2f}")
    col2.metric(f"Contractor Profit ({est['markup_percent']}%)", f"${est['markup_amount']:,.2f}")
    col3.metric("Client Invoice Total", f"${est['grand_total']:,.2f}")

    st.divider()

    # Material Table
    table_rows = []
    for m in est["materials"]:
        line_total = m["qty"] * m["unit_price"]
        client_line_total = line_total * (1 + est["markup_percent"] / 100.0)
        table_rows.append({
            "Trade": m["trade"],
            "Item Description": m["name"],
            "Qty": f"{m['qty']} {m['unit']}",
            "Contractor Unit": f"${m['unit_price']:.2f}",
            "Contractor Cost": f"${line_total:.2f}",
            "Client Total": f"${client_line_total:.2f}",
        })

    st.subheader("📋 Itemized Bill of Materials")
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    # Printable Invoice Summary Box
    with st.expander("📄 View Client-Facing Summary (Ready for Print / Copy)"):
        st.markdown(f"""
        ### Estimate & Proposal
        * **Scope:** {', '.join(est['project_tags'])}
        * **Materials Total:** ${est['subtotal'] + est['markup_amount']:,.2f}
        * **Estimated Taxes ({est['tax_percent']}%):** ${est['tax_amount']:,.2f}
        ---
        ### **Grand Total: ${est['grand_total']:,.2f}**
        """)

    # PDF download
    pdf_bytes = build_invoice_pdf(est)
    st.download_button(
        "📥 Download Invoice (PDF)",
        data=pdf_bytes,
        file_name="estimate_invoice.pdf",
        mime="application/pdf",
    )

    # --- GOOGLE SHEETS SAVING LOGIC ---
    st.divider()
    if st.button("💾 Save Estimate to Google Sheets", type="secondary"):
        with st.spinner("Saving to cloud..."):
            try:
                gc = get_gspread_client()
                sheet = gc.open("Contractor Estimates").sheet1
                new_row = [
                    est["date"],
                    ", ".join(est["project_tags"]),
                    f"${est['subtotal']:.2f}",
                    f"${est['markup_amount']:.2f}",
                    f"${est['grand_total']:.2f}",
                ]
                sheet.append_row(new_row)
                st.success("✅ Estimate saved to Google Sheets successfully!")
            except Exception as e:
                st.error(f"Could not save to Google Sheets. Ensure your service-account "
                         f"credentials are configured and the sheet is shared. Error: {e}")
