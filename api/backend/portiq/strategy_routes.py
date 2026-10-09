from flask import Blueprint, jsonify, request, current_app
from backend.db_connection import get_db
from mysql.connector import Error

# A Blueprint for strategy routes
strategy_routes = Blueprint("strategy_routes", __name__)


# GET /strategies
# Return all strategies
@strategy_routes.route("/", methods=["GET"])
def get_all_strategies():
    cursor = get_db().cursor(dictionary=True)
    try:
        current_app.logger.info('GET /strategies')

        query = """
                SELECT *
                from Strategy
                """
        cursor.execute(query)
        strategies = cursor.fetchall()
        return jsonify(strategies), 200
    except Error as e:
        current_app.logger.error(f'Database error in get_all_strategies: {e}')
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()


# POST /strategies
# Create new strategy
@strategy_routes.route("/", methods=["POST"])
def create_strategy():
    cursor = get_db().cursor(dictionary=True)
    try:
        current_app.logger.info("POST /strategies")
        data = request.get_json()
        # Assign the next ID when the client doesn't supply one
        if not data.get("strategy_id"):
            cursor.execute("SELECT COALESCE(MAX(strategy_id), 0) + 1 AS next_id FROM Strategy")
            data["strategy_id"] = cursor.fetchone()["next_id"]

        query = """
                Insert INTO Strategy
                (strategy_name, strategy_type, created_at, parameter, status, strategy_id, trade_strat, port_strat)
                VALUES (%s,%s,NOW(),%s,%s,%s,%s,%s)
                """
        cursor.execute(query, (
            data['strategy_name'],
            data['strategy_type'],
            data['parameter'],
            data['status'],
            data['strategy_id'],
            data['trade_strat'],
            data['port_strat']
        ))
        get_db().commit()
        return jsonify({"message":"Strategy created", "strategy_id": data["strategy_id"]}), 200
    except Error as e:
        if e.errno == 1062:
            return jsonify({"error": f"Strategy ID {data['strategy_id']} already exists"}), 409
        current_app.logger.error(f"Database error in create_strategy: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()


# GET /strategies/<id>
# Return one strategy
@strategy_routes.route("/<int:strategy_id>", methods=["GET"])
def get_strategy(strategy_id):
    cursor = get_db().cursor(dictionary=True)
    try:
        current_app.logger.info(f"GET /strategies/{strategy_id}")
        query = """
                SELECT *
                from Strategy
                where strategy_id = %s
                """
        cursor.execute(query, (strategy_id,))
        result = cursor.fetchone()
        if not result:
            return jsonify({"error": "Strategy Not Found"}), 404
        return jsonify(result), 200
    except Error as e:
        current_app.logger.error(f"Database error in get_strategy: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()

# PUT /strategies/<id>
# Update parameter or status
@strategy_routes.route("/<int:strategy_id>", methods=["PUT"])
def update_strategy(strategy_id):
    cursor = get_db().cursor(dictionary=True)
    try:
        current_app.logger.info(f"PUT /strategies/{strategy_id}")
        data = request.get_json()
        if "parameter" in data:
            cursor.execute(
                "UPDATE Strategy SET parameter = %s where strategy_id=%s",
                (data["parameter"], strategy_id)
            )
        elif "status" in data:
            cursor.execute(
                "UPDATE Strategy SET status =%s where strategy_id=%s",
                (data["status"],strategy_id)
            )
        else:
            return jsonify({"error": "Provide parameter or status"}), 400
        get_db().commit()
        return jsonify({"message": "Updated"}), 200
    except Error as e:
        current_app.logger.error(f"Database error in update_strategy: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()

# GET /strategies/<id>/performance
# Return performance for strategy
@strategy_routes.route("/<int:strategy_id>/performance", methods=["GET"])
def get_strategy_performance(strategy_id):
    cursor = get_db().cursor(dictionary=True)
    try:
        current_app.logger.info(f'GET /strategies/{strategy_id}/performance')

        query = """
                SELECT *
                from PerformanceRecord
                where strat_perf = %s
                """
        cursor.execute(query, (strategy_id,))
        performance = cursor.fetchall()
        if not performance:
            return jsonify({"error": "Performance not found"}), 404
        return jsonify(performance), 200
    except Error as e:
        current_app.logger.error(f'Database error in get_strategy_performance: {e}')
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()


# GET /strategies/<id>/benchmark
# Return benchmark for strategy
@strategy_routes.route("/<int:strategy_id>/benchmark", methods=["GET"])
def get_strategy_benchmark(strategy_id):
    cursor = get_db().cursor(dictionary=True)
    try:
        current_app.logger.info(f'GET /strategies/{strategy_id}/benchmark')

        query = """
                SELECT *
                from Benchmark
                where strat_bench = %s
                """
        cursor.execute(query, (strategy_id,))
        benchmark = cursor.fetchall()
        if not benchmark:
            return jsonify({"error": "Benchmark not found"}), 404
        return jsonify(benchmark), 200
    except Error as e:
        current_app.logger.error(f'Database error in get_strategy_benchmark: {e}')
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
