"""
Backend - Monitor de Liquidações (AAVE v3 + Kamino)
Usa API GraphQL oficial do Aave: https://api.v3.aave.com/graphql
"""

from flask import Flask, jsonify
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

AAVE_GRAPHQL = "https://api.v3.aave.com/graphql"
KAMINO_API = "https://api.kamino.finance"

CHAIN_IDS = {
    "ethereum": 1,
    "arbitrum": 42161,
    "polygon": 137,
}

USER_POSITIONS_QUERY = """
query UserPositions($address: String!, $chainId: Int!) {
  userPositions(address: $address, chainId: $chainId) {
    collateral {
      asset { symbol decimals priceUSD }
      balance
      isCollateral
      liquidationThreshold
    }
    borrows {
      asset { symbol decimals priceUSD }
      balance
      totalDebtUSD
    }
    summary {
      totalCollateralUSD
      totalBorrowsUSD
      healthFactor
    }
  }
}
"""


def fetch_aave(network, address):
    chain_id = CHAIN_IDS.get(network)
    if not chain_id:
        return None, f"Rede '{network}' não suportada"

    try:
        r = requests.post(
            AAVE_GRAPHQL,
            json={
                "query": USER_POSITIONS_QUERY,
                "variables": {"address": address, "chainId": chain_id}
            },
            timeout=15,
            headers={"Content-Type": "application/json"}
        )
        data = r.json()

        if "errors" in data:
            # Fallback: tenta query alternativa simples
            return fetch_aave_simple(network, address)

        user_data = data.get("data", {}).get("userPositions")
        if not user_data:
            return fetch_aave_simple(network, address)

        collaterals = user_data.get("collateral", [])
        borrows = user_data.get("borrows", [])
        summary = user_data.get("summary", {})

        total_debt_usd = float(summary.get("totalBorrowsUSD", 0))

        positions = []
        for col in collaterals:
            if not col.get("isCollateral"):
                continue
            asset = col.get("asset", {})
            balance = float(col.get("balance", 0))
            liq_threshold = float(col.get("liquidationThreshold", 0)) / 10000
            price = float(asset.get("priceUSD", 0))

            if balance > 0 and liq_threshold > 0 and total_debt_usd > 0:
                liq_price = total_debt_usd / (balance * liq_threshold)
            else:
                liq_price = 0

            distance_pct = ((price - liq_price) / price * 100) if price > 0 and liq_price > 0 else 100
            status = "danger" if distance_pct < 15 else "warn" if distance_pct < 30 else "ok"

            positions.append({
                "asset": asset.get("symbol", "?"),
                "liqPrice": round(liq_price, 2),
                "currentPrice": round(price, 2),
                "distancePct": round(distance_pct, 1),
                "status": status,
            })

        return positions, round(total_debt_usd, 2)

    except Exception as e:
        return fetch_aave_simple(network, address)


def fetch_aave_simple(network, address):
    """Fallback: query simples de userReserves via gateway do The Graph com API key pública"""
    GATEWAY_URLS = {
        "ethereum": "https://gateway.thegraph.com/api/subgraphs/id/JCNWRypm7FYwV8fx5HhzZPSFaMxgkPuw4TnR3Gpi81zk",
        "arbitrum": "https://gateway.thegraph.com/api/subgraphs/id/4xyasjQeREe7PxnF6wVdobZvCw5mhoHZq3T7guRpuNPf",
        "polygon":  "https://gateway.thegraph.com/api/subgraphs/id/Ab-HBHMn9hEKfnhrjWorQBNs2BEFkPnEKRimGrELyFnA",
    }

    url = GATEWAY_URLS.get(network)
    if not url:
        return [], 0

    query = """
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
    """ % address.lower()

    try:
        r = requests.post(url, json={"query": query}, timeout=15, headers={"Content-Type": "application/json"})
        data = r.json()
        if "errors" in data:
            return [], 0

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
            if (var_debt + stable_debt) > 0 and price > 0:
                total_debt_usd += (var_debt + stable_debt) * price

            atoken = float(res.get("currentATokenBalance", 0)) / 10**decimals
            if res.get("usageAsCollateralEnabledOnUser") and atoken > 0 and liq_threshold > 0:
                collaterals.append({"symbol": symbol, "balance": atoken, "liqThreshold": liq_threshold, "priceUSD": price})

        positions = []
        for col in collaterals:
            liq_price = total_debt_usd / (col["balance"] * col["liqThreshold"]) if total_debt_usd > 0 else 0
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
