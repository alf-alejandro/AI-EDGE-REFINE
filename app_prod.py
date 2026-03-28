"""
app_prod.py — Entry point PRODUCCIÓN
Flask web server + hedge_live_prod en hilo background.
IMPORTANTE: El bot arranca PAUSADO. Activar manualmente vía /api/start.

Endpoints:
  GET  /               Dashboard HTML
  GET  /api/state      Estado actual (JSON)
  GET  /api/trades.csv Descarga CSV de trades
  POST /api/start      Iniciar bot (activar trading real)
  POST /api/stop       Detener bot
"""

import csv
import io
import os
from datetime import datetime
from flask import Flask, jsonify, render_template, Response

import hedge_live_prod as hedge

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 8080))


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(hedge.get_state_snapshot())


@app.route("/api/start", methods=["POST"])
def api_start():
    ok, msg = hedge.start()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = hedge.stop()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/trades.csv")
def api_csv():
    trades = hedge.estado["trades"]
    fields = [
        "ts", "tipo", "lado1_side", "lado1_usd", "lado1_precio",
        "hedgeado", "lado2_side", "lado2_usd", "lado2_precio",
        "exit_precio", "resolucion", "pnl", "capital", "outcome", "secs_entrada",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)
    filename = f"hedge_prod_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    # El hilo del bot se inicia pero arranca PAUSADO
    # Debes ir al dashboard y presionar INICIAR para activar el trading real
    hedge.start()          # inicia hilo
    hedge.estado["paused"] = True  # asegurar pausa

    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
