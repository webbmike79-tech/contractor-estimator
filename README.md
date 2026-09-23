# Contractor Material Estimator & Invoice Generator

A Streamlit web app that turns freeform job descriptions into material takeoffs and client-ready invoices for remodel contractors (decks, bathrooms, kitchens, fences, flooring, painting).

## How it works
1. Describe the job in plain English (e.g. "a 5x5 deck with treated wood, handrails, and a ramp").
2. Gemini (`gemini-3.6-flash`, structured JSON output) extracts the trade type and dimensions.
3. Built-in takeoff logic computes quantities and costs from an editable per-item price table.
4. The app shows wholesale cost, contractor profit, and the client invoice total, with an itemized bill of materials, a printable summary, and a downloadable PDF invoice.
5. Optionally save each estimate as a row in a Google Sheet.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="your-key-here"
streamlit run app.py
```

## Google Sheets saving (optional)
1. Create a Google Cloud project; enable the Google Sheets API and Google Drive API.
2. Create a service account, download its JSON key as `secrets.json` next to `app.py`.
3. Share a spreadsheet named **Contractor Estimates** with the service account email as Editor.
   Header row: `Date | Job Scope | Material Cost | Estimated Profit | Client Invoice Total`.

## Notes
- Adjustable in the sidebar: contractor markup (default 20%), sales tax (default 7%),
  and every unit price (sidebar → "Edit Unit Prices").
- The estimate persists in session state, so the PDF download and Sheets buttons
  stay available after reruns.
- The LLM call retries automatically up to 3 times on 503 overload errors.
- `secrets.json` is gitignored — never commit credentials. On Streamlit Cloud,
  use `st.secrets["gcp_service_account"]` instead (supported automatically).
