import math
import os
import time
import warnings
from typing import List, Optional
from google import genai
from google.genai import types
from google.genai.errors import ServerError
from pydantic import BaseModel, Field
import streamlit as st
import gspread
from datetime import datetime

# Silence background warnings
warnings.filterwarnings("ignore")

# -------------------------------------------------------------
# 1. PAGE SETUP
# -------------------------------------------------------------
st.set_page_config(
    page_title="Contractor Remodel Estimator",
    page_icon="🔨",
    layout="wide",
)

API_KEY = os.environ.get(
    "GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE"
)
client = genai.Client(api_key=API_KEY)

# -------------------------------------------------------------
# 2. DATA SCHEMAS
# -------------------------------------------------------------
class SingleProject(BaseModel):
    trade: str = Field(
        description="The type of job: 'deck', 'bathroom', or 'kitchen'."
    )
    length_ft: Optional[float] = Field(default=0.0, description="Length in feet")
    width_ft: Optional[float] = Field(default=0.0, description="Width in feet")
    linear_ft: Optional[float] = Field(
        default=0.0, description="Linear feet of cabinets, railings, etc."
    )
    has_handrails: Optional[bool] = Field(default=False)
    has_ramp: Optional[bool] = Field(default=False)


class ContractorPromptExtraction(BaseModel):
    projects: List[SingleProject]


# -------------------------------------------------------------
# 3. TAKEOFF CALCULATIONS
# -------------------------------------------------------------
def calculate_deck(proj: SingleProject):
    l = proj.length_ft or 10
    w = proj.width_ft or 10
    waste = 1.10

    joists_count = math.ceil(((w * 12) / 16) + 1)
    deck_boards = math.ceil(((l * 12) / 5.5) * waste)

    items = [
        {"name": f"2x6x{int(l)}' Treated Framing Joists", "qty": joists_count, "unit": "pcs", "unit_price": 9.50},
        {"name": f"5/4x6x{int(w)}' Treated Deck Boards", "qty": deck_boards, "unit": "pcs", "unit_price": 11.25},
        {"name": "4x4x8' Treated Support Posts", "qty": 4, "unit": "pcs", "unit_price": 14.00},
        {"name": "3-inch Exterior Deck Screws (5 lb box)", "qty": 1, "unit": "box", "unit_price": 32.00},
    ]
    if proj.has_handrails:
        items.append({"name": "2x4x8' Handrail Lumber", "qty": 6, "unit": "pcs", "unit_price": 6.75})
        items.append({"name": "2x2x36\" Wood Balusters", "qty": 35, "unit": "pcs", "unit_price": 1.80})
    if proj.has_ramp:
        items.append({"name": "Ramp Stringers & Decking Kit", "qty": 1, "unit": "kit", "unit_price": 165.00})
    return items


def calculate_bathroom(proj: SingleProject):
    l = proj.length_ft or 8
    w = proj.width_ft or 5
    floor_sqft = l * w
    wet_wall_sqft = 15 * 8

    return [
        {"name": "1/2\" x 3'x5' Cement Backer Boards", "qty": math.ceil((wet_wall_sqft / 15) * 1.1), "unit": "sheets", "unit_price": 15.50},
        {"name": "Waterproofing Membrane (1 Gal)", "qty": 1, "unit": "bucket", "unit_price": 58.00},
        {"name": "Wall Tile (12x24 Porcelain)", "qty": round(wet_wall_sqft * 1.15), "unit": "sq ft", "unit_price": 4.50},
        {"name": "Floor Tile (Mosaic)", "qty": round(floor_sqft * 1.15), "unit": "sq ft", "unit_price": 6.20},
        {"name": "Shower Valve & Trim Kit", "qty": 1, "unit": "kit", "unit_price": 185.00},
    ]


def calculate_kitchen(proj: SingleProject):
    cabinets_lf = proj.linear_ft or 15
    countertop_sqft = cabinets_lf * 2.1

    return [
        {"name": "Standard Base Cabinets (avg 30\")", "qty": math.ceil(cabinets_lf / 2.5), "unit": "units", "unit_price": 240.00},
        {"name": "Standard Wall Cabinets (30\" H)", "qty": math.ceil(cabinets_lf / 2.5), "unit": "units", "unit_price": 195.00},
        {"name": "Quartz Countertop Slab", "qty": round(countertop_sqft * 1.1), "unit": "sq ft", "unit_price": 55.00},
        {"name": "Subway Tile Backsplash", "qty": round(cabinets_lf * 1.5 * 1.15), "unit": "sq ft", "unit_price": 4.75},
        {"name": "Sink & Pull-down Faucet Kit", "qty": 1, "unit": "kit", "unit_price": 260.00},
    ]


# -------------------------------------------------------------
# 4. LLM CALL WITH RETRY LOGIC (Handles 503 errors gracefully)
# -------------------------------------------------------------
def parse_with_ai(user_prompt: str, max_retries: int = 3):
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
# 5. UI LAYOUT
# -------------------------------------------------------------
st.title("🔨 Contractor Material Estimator & Invoice Generator")
st.caption("Enter freeform job specs to extract dimensions, run material takeoffs, and generate invoices.")

# Sidebar Settings
with st.sidebar:
    st.header("⚙️ Pricing Settings")
    markup_percent = st.slider("Contractor Markup (%)", min_value=0, max_value=50, value=20, step=5)
    tax_percent = st.slider("Estimated Sales Tax (%)", min_value=0.0, max_value=12.0, value=7.0, step=0.5)
    st.divider()
    st.info("💡 **Quick Examples:**\n\n- *5x5 deck with treated wood, handrails, and a ramp*\n- *8x10 bathroom remodel*\n- *Kitchen remodel with 20ft of cabinets*")

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
                st.error(f"Google API is currently overloaded or unavailable. Please try clicking again in a few seconds. ({e})")
                st.stop()

        all_materials = []
        project_tags = []

        for proj in data.projects:
            trade_clean = proj.trade.lower().strip()
            project_tags.append(trade_clean.upper())

            if "deck" in trade_clean:
                items = calculate_deck(proj)
            elif "bath" in trade_clean:
                items = calculate_bathroom(proj)
            elif "kitchen" in trade_clean:
                items = calculate_kitchen(proj)
            else:
                continue

            for itm in items:
                itm["trade"] = trade_clean.upper()
                all_materials.append(itm)

        if not all_materials:
            st.error("No recognized projects (Deck, Bath, or Kitchen) found. Try specifying dimensions or room types.")
        else:
            # Summary Metrics
            cost_subtotal = sum(m["qty"] * m["unit_price"] for m in all_materials)
            markup_amount = cost_subtotal * (markup_percent / 100.0)
            tax_amount = (cost_subtotal + markup_amount) * (tax_percent / 100.0)
            grand_total = cost_subtotal + markup_amount + tax_amount

            st.success(f"Recognized Scope: **{', '.join(project_tags)}**")

            col1, col2, col3 = st.columns(3)
            col1.metric("Wholesale Material Cost", f"${cost_subtotal:,.2f}")
            col2.metric(f"Contractor Profit ({markup_percent}%)", f"${markup_amount:,.2f}")
            col3.metric("Client Invoice Total", f"${grand_total:,.2f}")

            st.divider()

            # Material Table
            table_rows = []
            for m in all_materials:
                line_total = m["qty"] * m["unit_price"]
                client_line_total = line_total * (1 + markup_percent / 100.0)
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
                * **Scope:** {', '.join(project_tags)}
                * **Materials Total:** ${cost_subtotal + markup_amount:,.2f}
                * **Estimated Taxes ({tax_percent}%):** ${tax_amount:,.2f}
                ---
                ### **Grand Total: ${grand_total:,.2f}**
                """)

            # --- GOOGLE SHEETS SAVING LOGIC ---
            st.divider()
            if st.button("💾 Save Estimate to Google Sheets", type="secondary"):
                with st.spinner("Saving to cloud..."):
                    try:
                        # 1. Connect to Google using your secrets file
                        gc = gspread.service_account(filename="secrets.json")

                        # 2. Open the specific spreadsheet by its name
                        sheet = gc.open("Contractor Estimates").sheet1

                        # 3. Create a list of the data for the new row
                        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
                        new_row = [
                            current_date,
                            ", ".join(project_tags),  # The jobs recognized (e.g., DECK, KITCHEN)
                            f"${cost_subtotal:.2f}",
                            f"${markup_amount:.2f}",
                            f"${grand_total:.2f}"
                        ]

                        # 4. Append it to the next empty row in the sheet
                        sheet.append_row(new_row)

                        st.success("✅ Estimate saved to Google Sheets successfully!")
                    except Exception as e:
                        st.error(f"Could not save to Google Sheets. Ensure secrets.json is correct and the sheet is shared. Error: {e}")
