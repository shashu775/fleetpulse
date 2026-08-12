"""Shipping label rendering.

Returns plain HTML so you can open it in a browser and Ctrl+P to print it.
Real carriers generate ZPL or PDF; HTML keeps the dependency count at zero
and demos just as well.
"""

from html import escape


def render_label(d: dict) -> str:
    """Render a printable shipping label for one waybill."""
    cod_block = (
        f'<div class="cod">COD &#8377;{d["cod_amount"]:,.2f}</div>'
        if d["payment_mode"] == "COD"
        else '<div class="prepaid">PREPAID &mdash; DO NOT COLLECT</div>'
    )

    # escape() everything that came from user input -- a consignee name
    # containing "<script>" should render as text, not execute.
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Label {escape(d["awb"])}</title>
<style>
  body {{ font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
          background: #f4f4f5; padding: 24px; }}
  .label {{ border: 3px solid #000; width: 420px; padding: 18px;
            background: #fff; }}
  .brand {{ text-align: center; font-weight: 700; letter-spacing: 3px;
            font-size: 15px; }}
  .awb {{ font-size: 27px; font-weight: 700; letter-spacing: 2px;
          margin: 6px 0; }}
  .route {{ font-size: 15px; font-weight: 700; }}
  .row {{ margin: 7px 0; font-size: 13px; line-height: 1.45; }}
  .k {{ font-weight: 700; }}
  hr {{ border: none; border-top: 2px dashed #000; margin: 12px 0; }}
  .cod {{ background: #000; color: #fff; padding: 9px; font-weight: 700;
          text-align: center; font-size: 17px; }}
  .prepaid {{ border: 2px solid #000; padding: 9px; text-align: center;
              font-weight: 700; font-size: 13px; }}
  .status {{ font-size: 12px; color: #444; }}
</style>
</head>
<body>
  <div class="label">
    <div class="brand">FLEETPULSE LOGISTICS</div>
    <hr>
    <div class="awb">{escape(d["awb"])}</div>
    <div class="route">{escape(d["origin_hub"])} &rarr; {escape(d["destination_hub"])}</div>
    <hr>
    <div class="row"><span class="k">TO:</span> {escape(d["consignee_name"])}</div>
    <div class="row">{escape(d["consignee_addr"])}</div>
    <div class="row"><span class="k">PH:</span> {escape(d["consignee_phone"])}</div>
    <hr>
    <div class="row"><span class="k">FROM:</span> {escape(d["merchant_name"])}</div>
    <div class="row"><span class="k">WT:</span> {d["weight_grams"]} g</div>
    <div class="row status">Status: {escape(d["current_status"])}</div>
    <hr>
    {cod_block}
  </div>
</body>
</html>"""
