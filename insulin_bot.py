import logging
import os
import json
import speech_recognition as sr
from datetime import datetime
from pydub import AudioSegment
from typing import Union, Dict, List

import openai

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- Basic Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client from environment variable
try:
    client = openai.OpenAI()
except openai.OpenAIError:
    logger.error("FATAL: OPENAI_API_KEY environment variable not set.")
    exit()

# --- Conversation States ---
# Removed CONFIRMING_CARBS state
ASKING_FOOD, ASKING_INSULIN = range(2)

# --- In-memory Storage ---
event_records: list[Dict] = []
user_chat_id: int = None


# --- OpenAI API Functions ---

async def get_openai_json_response(prompt: str) -> dict:
    """Sends a request to OpenAI and expects a JSON object in return."""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a helpful assistant designed to output JSON."},
                {"role": "user", "content": prompt}
            ]
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Error calling OpenAI for JSON response: {e}")
        return {}

async def get_openai_chat_response(messages: List[Dict]) -> str:
    """Sends a conversational history to OpenAI for a chat response."""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error calling OpenAI for chat response: {e}")
        return "Sorry, I encountered an error and can't respond right now."

# --- Record Mode Conversation Handlers ---

async def trigger_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global user_chat_id
    user_chat_id = update.message.chat_id
    logger.info(f"Triggering alert for chat_id: {user_chat_id}")
    await context.bot.send_message(
        chat_id=user_chat_id,
        text="Hi, I've noticed a recent event. What did you eat for your last meal?"
    )
    return ASKING_FOOD

async def received_food(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Thanks! Analyzing...")
    user_input = await _get_text_from_message(update.message)
    if not user_input:
        await update.message.reply_text("I couldn't understand that. Please try typing it.")
        return ASKING_FOOD

    prompt = f'Analyze this food description: "{user_input}". Estimate total carbohydrates. Return JSON with one key, "carbs" (integer or null).'
    data = await get_openai_json_response(prompt)
    carbs = data.get("carbs")

    if carbs is not None:
        context.user_data['carbs'] = carbs
        # Directly ask for insulin after estimating carbs, removing the confirmation step
        await update.message.reply_text(
            f"Got it, I estimate that was about {carbs}g of carbs.\n\n"
            "Now, how many units of insulin did you inject?",
            reply_markup=ReplyKeyboardRemove()
        )
        return ASKING_INSULIN
    else:
        await update.message.reply_text("I had trouble estimating. Could you try describing it again?")
        return ASKING_FOOD

async def received_insulin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Checking the value...")
    user_input = await _get_text_from_message(update.message)
    if not user_input:
        await update.message.reply_text("I didn't catch that. Please type the number of units.")
        return ASKING_INSULIN

    prompt = f"Analyze input: \"{user_input}\". Is it a reasonable insulin dose (0-100 units)? Return JSON with one key, \"insulin_units\" (integer or null)."
    data = await get_openai_json_response(prompt)
    insulin = data.get("insulin_units")

    if insulin is not None:
        context.user_data['insulin'] = insulin
        new_record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "carbs": context.user_data.get('carbs', 'N/A'),
            "insulin": context.user_data.get('insulin', 'N/A')
        }
        event_records.append(new_record)
        await update.message.reply_text("Thank you! Information recorded.")
        context.user_data.clear()
        return ConversationHandler.END
    else:
        await update.message.reply_text("That doesn't seem like a valid amount. Please provide a simple number.")
        return ASKING_INSULIN

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    if 'chat_history' in context.user_data:
        del context.user_data['chat_history']
    return ConversationHandler.END

# --- Deep Dive Chat Mode Handler ---

async def handle_deep_dive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles general text messages for conversational Q&A."""
    user_message = update.message.text
    await update.message.reply_text("Thinking...")

    # Initialize chat history if it doesn't exist
    if 'chat_history' not in context.user_data:
        context.user_data['chat_history'] = []
    
    # Format the event logs into a string for the AI context
    log_summary = "No records on file."
    if event_records:
        log_summary = "Here are the user's past records:\n"
        for i, record in enumerate(event_records):
            log_summary += f"- Record {i+1} ({record['timestamp']}): Carbs={record['carbs']}g, Insulin={record['insulin']} units\n"

    # Construct the full message list for the AI
    system_prompt = (
        "You are a helpful diabetes assistant. Your role is to answer the user's questions based on their provided logs and conversation history. "
        "IMPORTANT: You are an AI assistant, NOT a medical professional. Always include a disclaimer that your advice is not a substitute for consultation with a doctor or endocrinologist. "
        "Analyze the provided records and chat history to give the most relevant, helpful, and safe response."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "system", "content": f"BACKGROUND INFO:\n{log_summary}"})
    messages.extend(context.user_data['chat_history'])
    messages.append({"role": "user", "content": user_message})

    # Get the response from OpenAI
    ai_response = await get_openai_chat_response(messages)
    await update.message.reply_text(ai_response)

    # Update the chat history with the latest exchange
    context.user_data['chat_history'].append({"role": "user", "content": user_message})
    context.user_data['chat_history'].append({"role": "assistant", "content": ai_response})
    # Limit history to last 10 messages (5 turns) to keep it manageable
    context.user_data['chat_history'] = context.user_data['chat_history'][-10:]

# --- Utility Functions and Commands ---

async def _get_text_from_message(message) -> Union[str, None]:
    """Helper to get text from either a text or voice message."""
    if message.text:
        return message.text
    if message.voice:
        return await transcribe_voice(message.voice)
    return None

async def transcribe_voice(voice_message) -> Union[str, None]:
    # (This function is unchanged)
    oga_path, wav_path = "", ""
    try:
        voice_file = await voice_message.get_file()
        oga_path = f"voice_{voice_message.file_unique_id}.oga"
        wav_path = f"voice_{voice_message.file_unique_id}.wav"
        await voice_file.download_to_drive(oga_path)
        audio = AudioSegment.from_ogg(oga_path)
        audio.export(wav_path, format="wav")
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            return recognizer.recognize_google(audio_data, language="en-US")
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return None
    finally:
        if os.path.exists(oga_path): os.remove(oga_path)
        if os.path.exists(wav_path): os.remove(wav_path)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global user_chat_id
    user_chat_id = update.message.chat_id
    await update.message.reply_text(
        "Hi! I'm your insulin monitoring assistant. 👋\n\n"
        "I can record events (use /trigger_alert to simulate) or you can ask me questions about your history.\n\n"
        "Use /show_records to see all data, and /clear_chat to reset our current conversation."
    )

async def show_records(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not event_records:
        await update.message.reply_text("There are no records yet.")
        return
    message = "--- Stored Records ---\n\n"
    for i, record in enumerate(event_records):
        message += (f"📝 **Record #{i + 1}**\n - Timestamp: {record['timestamp']}\n"
                    f" - Estimated Carbs: {record['carbs']} g\n - Insulin: {record['insulin']} units\n\n")
    await update.message.reply_text(message, parse_mode='Markdown')

async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears the short-term conversational history."""
    if 'chat_history' in context.user_data:
        del context.user_data['chat_history']
        await update.message.reply_text("Our current conversation history has been cleared.")
    else:
        await update.message.reply_text("There's no conversation history to clear.")

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN environment variable not set.")
        return

    application = Application.builder().token(token).build()

    # Handler for recording events (structured conversation)
    record_handler = ConversationHandler(
        entry_points=[CommandHandler("trigger_alert", trigger_alert)],
        states={
            # The confirmation state has been removed from the conversation flow
            ASKING_FOOD: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE, received_food)],
            ASKING_INSULIN: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE, received_insulin)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    # Handler for deep dive chat (unstructured text)
    deep_dive_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_deep_dive)

    # Add handlers to the application
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("show_records", show_records))
    application.add_handler(CommandHandler("clear_chat", clear_chat))
    
    # CRITICAL: The structured handler must be added BEFORE the general text handler
    application.add_handler(record_handler)
    application.add_handler(deep_dive_handler)
    
    print("Bot started with Deep Dive capabilities. Press Ctrl+C to stop.")
    application.run_polling()

if __name__ == "__main__":
    main()
