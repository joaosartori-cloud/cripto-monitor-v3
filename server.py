"""
Backend - Monitor de Liquidações (AAVE v3 + Kamino)
Usa GraphQL API pública do Aave (sem API key)
"""

from flask import Flask, jsonify
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

KAMINO_API = "https://api.kamino.finance"

# Subgraphs públicos do Aave v3 (hosted service — sem API key)
AAVE_SUBGRAPHS = {
    "ethereum": "https://api.thegraph.com/subgraphs/name/aave/protocol-v3",
    "arbitrum": "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum",
    "polygon":  "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-polygon",
}

QUERY = """
{
  userReserves(where: {user: "%s"}) {
    currentATokenBalance
    currentVariableDebt
    currentStableDebt
    usageAsCollateralEnabledOnUser
    reserve {
      symbol
      decimals
      reserveLiquidationThreshold
      priceInUSD
    }
  }
}
"""


def fetch_aave(network, address):
    url = AAVE_SUBGRAPHS.get(network)
    if not url:
        return None, f"Rede '{network}' não suportada"

    try:
        r = requests.post(
            url,
            json={"query": QUERY % address.lower()},
            timeout=15,
            headers={"Content-Type": "application/json"}
        )
        data = r.json()

        if "errors" in data:
            return None, str(data["errors"])

        reserves = data.get("data", {}).get("userReserves", [])
        if not reserves:
            return [], 0

        total_debt_usd = 0.0
        collaterals = []

        for res in reserves:
            reserve = res.get("reserve", {})
            decimals = int(reserve.get("decimals", 18))
            price = float(reserve.get("priceInUSD", 0))
            liq_threshold = float(reserve.get("reserveLiquidationThreshold", 0)) / 10000
            symbol = reserve.get("symbol", "?")

            var_debt = float(res.get("currentVariableDebt", 0)) / 10**decimals
            stable_debt = float(res.get("currentStableDebt", 0)) / 10**decimals
            total_debt = var_debt + stable_debt

            if total_debt > 0 and price > 0:
                total_debt_usd += total_debt * price

            atoken_bal = float(res.get("currentATokenBalance", 0)) / 10**decimals
            is_collateral = res.get("usageAsCollateralEnabledOnUser", False)

            if is_collateral and atoken_bal > 0 and liq_threshold > 0:
                collaterals.append({
                    "symbol": symbol,
                    "balance": atoken_bal,
                    "liqThreshold": liq_threshold,
                    "priceUSD": price,
                })

        positions = []
        for col in collaterals:
            if col["balance"] > 0 and col["liqThreshold"] > 0 and total_debt_usd > 0:
                liq_price = total_debt_usd / (col["balance"] * col["liqThreshold"])
            else:
                liq_price = 0

            current = col["priceUSD"]
            distance_pct = ((current - liq_price) / current * 100) if current > 0 and liq_price > 0 else 100
            status = "danger" if distance_pct < 15 else "warn" if distance_pct < 30 else "ok"

            positions.append({
                "asset": col["symbol"],
                "liqPrice": round(liq_price, 2),
                "currentPrice": round(current, 2),
                "distancePct": round(distance_pct, 1),
                "status": status,
            })

        return positions, round(total_debt_usd, 2)

    except Exception as e:
        return None, str(e)


@app.route("/aave/<network>/<address>")
def aave_positions(network, address):
    result, debt_or_err = fetch_aave(network.lower(), address)
    if result is None:
        return jsonify({"error": debt_or_err}), 500
    return jsonify({"positions": result, "totalDebtUSD": debt_or_err, "network": network})


@app.route("/kamino/<wallet>")
def kamino_positions(wallet):
    try:
        resp = requests.get(
            f"{KAMINO_API}/v2/users/{wallet}/obligations",
            timeout=15,
            headers={"Accept": "application/json"}
        )
        data = resp.json()
        obligations = data if isinstance(data, list) else data.get("obligations", [])

        if not obligations:
            return jsonify({"positions": [], "totalDebtUSD": 0})

        positions = []
        total_debt = 0.0

        for ob in obligations:
            borrows = ob.get("borrows", [])
            deposits = ob.get("deposits", [])
            ob_debt = sum(float(b.get("marketValueRefreshed", b.get("marketValue", 0))) for b in borrows)
            total_debt += ob_debt

            for dep in deposits:
                asset = dep.get("tokenSymbol") or dep.get("symbol", "Unknown")
                liq_price = float(dep.get("liquidationPrice") or dep.get("liq_price") or 0)
                current_price = float(dep.get("price") or dep.get("currentPrice") or 0)
                distance_pct = ((current_price - liq_price) / current_price * 100) if current_price > 0 and liq_price > 0 else 100
                status = "danger" if distance_pct < 15 else "warn" if distance_pct < 30 else "ok"

                positions.append({
                    "asset": asset,
                    "liqPrice": round(liq_price, 2),
                    "currentPrice": round(current_price, 2),
                    "distancePct": round(distance_pct, 1),
                    "status": status,
                })

        return jsonify({"positions": positions, "totalDebtUSD": round(total_debt, 2)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅  Backend rodando na porta {port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
