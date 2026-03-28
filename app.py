"""
app.py — Entry point SIMULACIÓN
Flask web server + hedge_sim en hilo background.

Endpoints:
  GET  /               Dashboard HTML
  GET  /api/state      Estado actual (JSON)
  GET  /api/trades.csv Descarga CSV de trades
  POST /api/start      Iniciar bot
  POST /api/stop       Detener bot
"""

import csv
import io
import os
import json
from datetime import datetime
from flask import Flask, jsonify, render_template, Response, request

import hedge_sim

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 8080))


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(hedge_sim.get_state_snapshot())


@app.route("/api/start", methods=["POST"])
def api_start():
    ok, msg = hedge_sim.start()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = hedge_sim.stop()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/trades.csv")
def api_csv():
    trades = hedge_sim.estado["trades"]
    fields = [
        "ts", "tipo", "lado1_side", "lado1_usd", "lado1_precio",
        "hedgeado", "lado2_side", "lado2_usd", "lado2_precio",
        "exit_precio", "resolucion", "pnl", "capital", "outcome", "secs_entrada",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)
    filename = f"hedge_sim_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    # Arrancar el bot automáticamente al iniciar
    hedge_sim.start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
