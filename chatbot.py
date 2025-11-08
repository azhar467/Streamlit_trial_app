import os
import streamlit as st
from langchain_google_genai import ChatGoogleGenerativeAI

# --- PAGE CONFIG ---
st.set_page_config(page_title="Gemini Chatbot", page_icon="🤖")

st.title("🤖 Gemini Chatbot (LangChain + Streamlit)")

# --- INPUT FOR API KEY ---
api_key = st.text_input("🔑 Enter your Google Gemini API Key:", type="password")

# --- SETUP FUNCTION ---
def get_gemini_llm(api_key):
    """Initialize Gemini model using LangChain."""
    os.environ["GOOGLE_API_KEY"] = api_key
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",  # latest available lightweight model
        temperature=0.7,
    )
    return llm

# --- MAIN CHAT SECTION ---
if api_key:
    llm = get_gemini_llm(api_key)

    # Keep conversation history
    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    # Display previous messages
    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Input for user message
    if prompt := st.chat_input("Type your message..."):
        # Show user message
        st.chat_message("user").markdown(prompt)
        st.session_state["messages"].append({"role": "user", "content": prompt})

        # Generate response
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                response = llm.invoke(prompt)
                reply = response.content if hasattr(response, "content") else str(response)
                st.markdown(reply)

        # Save assistant message
        st.session_state["messages"].append({"role": "assistant", "content": reply})
else:
    st.warning("Please enter your Gemini API key to start chatting.")
    st.success("chatbot is working!")

