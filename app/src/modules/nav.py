# Idea borrowed from https://github.com/fsmosca/sample-streamlit-authenticator

# This file has functions to add links to the left sidebar based on the user's role.

import streamlit as st


# ---- General ----------------------------------------------------------------

def home_nav():
    st.sidebar.page_link("Home.py", label="Home", icon="🏠")


def about_page_nav():
    st.sidebar.page_link("pages/30_About.py", label="About", icon="🧠")



# ---- Role: Quant_Trader ------------------------------------------------

def quant_trader_home_nav():
    st.sidebar.page_link(
        "pages/00_Quant_Trader_Home.py", label="Quantitative Trader Home", icon="👤"
    )


def world_bank_viz_nav():
    st.sidebar.page_link(
        "pages/01_World_Bank_Viz.py", label="World Bank Visualization", icon="🏦"
    )


def map_demo_nav():
    st.sidebar.page_link("pages/02_Map_Demo.py", label="Map Demonstration", icon="🗺️")


# ---- Role: data_analyst -----------------------------------------------------

def data_analyst_home_nav():
    st.sidebar.page_link(
        "pages/10_Data_Analyst_Home.py", label="Data Analyst Home", icon="👤"
    )


def ngo_directory_nav():
    st.sidebar.page_link("pages/14_NGO_Directory.py", label="NGO Directory", icon="📁")


def add_ngo_nav():
    st.sidebar.page_link("pages/15_Add_NGO.py", label="Add New NGO", icon="➕")


def prediction_nav():
    st.sidebar.page_link(
        "pages/11_Prediction.py", label="Regression Prediction", icon="📈"
    )


def api_test_nav():
    st.sidebar.page_link("pages/12_API_Test.py", label="Test the API", icon="🛜")


def classification_nav():
    st.sidebar.page_link(
        "pages/13_Classification.py", label="Classification Demo", icon="🌺"
    )

# ---- Role: beginner_user ----------------------------------------------------

def beginner_user_home_nav():
    st.sidebar.page_link(
        "pages/40_Beginner_User_Home.py", label="Beginner User Home", icon="👤"
    )


# ---- Role: CIO --------------------------------------------------------------

def cio_home_nav():
    st.sidebar.page_link("pages/23_CIO_Home.py", label="CIO Home", icon="🏠")


def cio_iam_nav():
    st.sidebar.page_link("pages/20_CIO_IAM.py", label="IAM", icon="🔐")


def cio_logs_nav():
    st.sidebar.page_link("pages/21_CIO_Logs.py", label="Logs", icon="📋")


def cio_llm_nav():
    st.sidebar.page_link("pages/22_CIO_LLM_Mgmt.py", label="LLM Management", icon="🤖")


# ---- Sidebar assembly -------------------------------------------------------

def SideBarLinks(show_home=False):
    """
    Renders sidebar navigation links based on the logged-in user's role.
    The role is stored in st.session_state when the user logs in on Home.py.
    """

    # Logo appears at the top of the sidebar on every page
    st.sidebar.image("assets/logo.png", width="stretch")

    # If no one is logged in, send them to the Home (login) page
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
        st.switch_page("Home.py")

    if show_home:
        home_nav()

    if st.session_state["authenticated"]:

        if st.session_state["role"] == "quant_trader":
            quant_trader_home_nav()
            st.sidebar.page_link(
                "pages/01_Portfolio_Performance.py",
                label="Portfolio Performance"
            )
            st.sidebar.page_link(
                "pages/02_Strategy_Benchmark.py",
                label="Strategy vs Benchmark"
            )
            st.sidebar.page_link(
                "pages/03_Risk_Analysis.py",
                label="Risk Analysis"
            )
            st.sidebar.page_link(
                "pages/04_Trading_Logs.py",
                label="Trading Logs"
            )
            st.sidebar.page_link(
                "pages/05_Market_Overview.py",
                label="Market Overview"
            )
            st.sidebar.page_link(
                "pages/06_Stock_Lookup.py",
                label="Stock Lookup & Backtest"
            )
        if st.session_state["role"] == "data_analyst":
            data_analyst_home_nav()
            st.sidebar.page_link(
                "pages/11_Dashboard_Layouts.py",
                label="Dashboard Layouts"
            )
            st.sidebar.page_link(
                "pages/12_Data_Cleaning.py",
                label="Data Cleaning"
            )
            st.sidebar.page_link(
                "pages/13_Datasets.py",
                label="Datasets"
            )
            st.sidebar.page_link(
                "pages/14_Visualizations.py",
                label="Visualizations"
            )


        if st.session_state["role"] == "beginner_user":
            beginner_user_home_nav()
            st.sidebar.page_link(
                "pages/41_Positions.py",
                label="Positions"
            )
            st.sidebar.page_link(
                "pages/42_Dashboard.py",
                label="Dashboard"
            )
            st.sidebar.page_link(
                "pages/43_Chat.py",
                label="Portfolio Assistant"
            )

        if st.session_state["role"] == "CIO":
            cio_home_nav()
            cio_iam_nav()
            cio_logs_nav()
            cio_llm_nav()

    # About link appears at the bottom for all roles
    about_page_nav()

    if st.session_state["authenticated"]:
        if st.sidebar.button("Logout"):
            del st.session_state["role"]
            del st.session_state["authenticated"]
            st.switch_page("Home.py")
