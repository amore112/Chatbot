import os
import asyncio
import tempfile
import sqlite3
import logging
from datetime import datetime, timedelta
from whatsapp_web_js import Client, LocalAuth, MessageMedia
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
from PyPDF2 import PdfReader
from ratelimit import limits, sleep_and_retry

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize AI clients
openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
anthropic_client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Initialize WhatsApp client
whatsapp_client = Client(LocalAuth())

# Initialize SQLite database
conn = sqlite3.connect('user_data.db')
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users
             (id TEXT PRIMARY KEY, provider TEXT, last_request TIMESTAMP)''')
c.execute('''CREATE TABLE IF NOT EXISTS conversations
             (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, role TEXT, content TEXT, timestamp TIMESTAMP)''')
conn.commit()

# Rate limiting: 5 requests per minute per user
@sleep_and_retry
@limits(calls=5, period=60)
def check_rate_limit(user_id):
    pass

async def extract_text_from_pdf(file_path):
    try:
        with open(file_path, 'rb') as file:
            pdf = PdfReader(file)
            text = ""
            for page in pdf.pages:
                text += page.extract_text()
        return text
    except Exception as e:
        logging.error(f"Error extracting text from PDF: {str(e)}")
        raise

async def get_user_data(user_id):
    c.execute("SELECT provider, last_request FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    if result:
        return {"provider": result[0], "last_request": result[1]}
    else:
        c.execute("INSERT INTO users (id, provider, last_request) VALUES (?, ?, ?)",
                  (user_id, "openai", datetime.now()))
        conn.commit()
        return {"provider": "openai", "last_request": datetime.now()}

async def update_user_data(user_id, provider):
    c.execute("UPDATE users SET provider = ?, last_request = ? WHERE id = ?",
              (provider, datetime.now(), user_id))
    conn.commit()

async def get_conversation_history(user_id, limit=10):
    c.execute("SELECT role, content FROM conversations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
              (user_id, limit))
    return [{"role": role, "content": content} for role, content in c.fetchall()][::-1]

async def add_to_conversation(user_id, role, content):
    c.execute("INSERT INTO conversations (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
              (user_id, role, content, datetime.now()))
    conn.commit()

async def get_ai_response(user_id, message, pdf_content=None):
    try:
        check_rate_limit(user_id)
    except Exception as e:
        logging.warning(f"Rate limit exceeded for user {user_id}")
        return "You've reached the rate limit. Please try again later."

    user_data = await get_user_data(user_id)
    conversation = await get_conversation_history(user_id)

    if message.lower().startswith("!switch"):
        parts = message.split()
        if len(parts) > 1:
            new_provider = parts[1].lower()
            if new_provider in ["openai", "claude"]:
                await update_user_data(user_id, new_provider)
                return f"Switched to {new_provider}"
            else:
                return "Invalid provider. Use 'openai' or 'claude'."

    if pdf_content:
        await add_to_conversation(user_id, "system", f"The user has provided a PDF with the following content:\n\n{pdf_content}\n\nPlease use this information as context for the conversation.")

    await add_to_conversation(user_id, "user", message)

    try:
        if user_data["provider"] == "openai":
            response = await openai_client.chat.completions.create(
                model="gpt-4",
                messages=conversation + [{"role": "user", "content": message}]
            )
            ai_message = response.choices[0].message.content
        elif user_data["provider"] == "claude":
            conversation_text = "\n\n".join([f"{'Human' if msg['role'] == 'user' else 'Assistant' if msg['role'] == 'assistant' else 'System'}: {msg['content']}" for msg in conversation + [{"role": "user", "content": message}]])
            response = await anthropic_client.completions.create(
                model="claude-2",
                prompt=f"{conversation_text}\n\nAssistant:",
                max_tokens_to_sample=300
            )
            ai_message = response.completion

        await add_to_conversation(user_id, "assistant", ai_message)
        return ai_message
    except Exception as e:
        logging.error(f"Error getting AI response: {str(e)}")
        return "I'm sorry, but I encountered an error while processing your request. Please try again later."

@whatsapp_client.on("message")
async def handle_message(message):
    user_id = message.from_
    
    if message.hasMedia and message.type == 'document' and message.mimetype == 'application/pdf':
        try:
            media = await message.downloadMedia()
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
                temp_file.write(media.buffer)
                temp_file_path = temp_file.name
            
            pdf_content = await extract_text_from_pdf(temp_file_path)
            os.unlink(temp_file_path)  # Delete the temporary file
            
            response = await get_ai_response(user_id, "I've uploaded a PDF. Please use its content as context for our conversation.", pdf_content)
            await message.reply("I've processed the PDF you sent. You can now ask questions about its content.")
        except Exception as e:
            logging.error(f"Error processing PDF: {str(e)}")
            await message.reply("I'm sorry, but I encountered an error while processing your PDF. Please try again later.")
    elif message.body.startswith("!ai"):
        query = message.body[4:].strip()
        response = await get_ai_response(user_id, query)
        await message.reply(response)

async def main():
    logging.info("Starting WhatsApp AI Chatbot...")
    await whatsapp_client.initialize()
    logging.info("WhatsApp client initialized. Scan the QR code to log in.")
    
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        conn.close()

if __name__ == "__main__":
    asyncio.run(main())

