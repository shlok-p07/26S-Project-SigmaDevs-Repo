from flask import Flask
from dotenv import load_dotenv
import os
import logging

from backend.db_connection import init_app as init_db

from backend.users.users_routes import users
from backend.roles.roles_routes import roles
from backend.actions.actions_routes import actions
from backend.portiq.asset_routes import asset_routes
from backend.portiq.ratings_routes import analystrating_routes
from backend.portiq.benchmark_routes import benchmark_routes
from backend.portiq.chatsession_routes import chatsession_routes
from backend.portiq.dailysummary_routes import dailysummary_routes
from backend.portiq.portfolio_routes import portfolio_routes
from backend.portiq.riskmetric_routes import riskmetric_routes
from backend.portiq.stockposition_routes import stockposition_routes
from backend.portiq.strategy_routes import strategy_routes
from backend.portiq.trade_routes import trade_routes
from backend.portiq.quant_routes import quant_routes
from backend.portiq.market_routes import market_routes
from backend.llm.llm_routes import llm
from backend.portiq.johndata_api_calls.dataset_routes import dataset_routes
from backend.portiq.johndata_api_calls.dashboardlayout_routes import dashboardlayout_routes
from backend.portiq.johndata_api_calls.datacleaning_routes import datacleaning_routes
from backend.portiq.johndata_api_calls.visualization_routes import visualization_routes
from backend.portiq.chat_routes import chat_routes


def create_app():
    app = Flask(__name__)

    app.logger.setLevel(logging.DEBUG)
    app.logger.info('API startup')

    # Load environment variables from the .env file so they are
    # accessible via os.getenv() below.
    load_dotenv()

    # Secret key used by Flask for securely signing session cookies.
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")

    # Database connection settings — values come from the .env file.
    app.config["MYSQL_DATABASE_USER"] = os.getenv("DB_USER").strip()
    app.config["MYSQL_DATABASE_PASSWORD"] = os.getenv("MYSQL_ROOT_PASSWORD").strip()
    app.config["MYSQL_DATABASE_HOST"] = os.getenv("DB_HOST").strip()
    app.config["MYSQL_DATABASE_PORT"] = int(os.getenv("DB_PORT").strip())
    app.config["MYSQL_DATABASE_DB"] = os.getenv("DB_NAME").strip()

    # Register the cleanup hook for the database connection.
    app.logger.info("create_app(): initializing database connection")
    init_db(app)

    # Register the routes from each Blueprint with the app object
    # and give a url prefix to each.
    app.logger.info("create_app(): registering blueprints")
    app.register_blueprint(users, url_prefix="/user")
    app.register_blueprint(roles, url_prefix="/role")
    app.register_blueprint(actions, url_prefix="/action")
    app.register_blueprint(asset_routes, url_prefix="/asset")
    app.register_blueprint(analystrating_routes, url_prefix="/AnalystRating")
    app.register_blueprint(benchmark_routes, url_prefix="/benchmark")
    app.register_blueprint(chatsession_routes, url_prefix="/ChatSession")
    app.register_blueprint(dailysummary_routes, url_prefix="/DailySummary")
    app.register_blueprint(portfolio_routes, url_prefix="/portfolios")
    app.register_blueprint(riskmetric_routes, url_prefix="/riskmetrics")
    app.register_blueprint(stockposition_routes, url_prefix="/StockPosition")
    app.register_blueprint(strategy_routes, url_prefix="/strategies")
    app.register_blueprint(trade_routes, url_prefix="/trades")
    app.register_blueprint(quant_routes, url_prefix="/quant")
    app.register_blueprint(market_routes, url_prefix="/quant")
    app.register_blueprint(llm, url_prefix="/llm")
    app.register_blueprint(dataset_routes, url_prefix="/datasets")
    app.register_blueprint(dashboardlayout_routes, url_prefix="/dashboardlayouts")
    app.register_blueprint(datacleaning_routes, url_prefix="/datacleaningmethods")
    app.register_blueprint(visualization_routes, url_prefix="/visualizations")
    app.register_blueprint(chat_routes, url_prefix="/chat")

    return app
