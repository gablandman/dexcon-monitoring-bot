import logging
import os
import json
import speech_recognition as sr
from datetime import datetime
from pydub import AudioSegment
from typing import Union, Dict, List

import openai

from telegram import Update, ReplyKeyboardRemove
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
    logger.error("FATAL: OPENAI_API_KEY environment variable is not set.")
    exit()

# --- Conversation States (Simplified) ---
AWAITING_INFO = 0

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
    """Sends a conversational history to OpenAI for a text response."""
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
    """Simulates an alert and asks the combined question."""
    global user_chat_id
    user_chat_id = update.message.chat_id
    logger.info(f"Triggering alert for chat_id: {user_chat_id}")
    await context.bot.send_message(
        chat_id=user_chat_id,
        text=(
            "Hi, I've noticed a recent event.\n\n"
            "What did you eat and how many units of insulin did you inject?"
        )
    )
    return AWAITING_INFO

async def received_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the user's combined response."""
    await update.message.reply_text("Thanks! Analyzing...")
    user_input = await _get_text_from_message(update.message)
    if not user_input:
        await update.message.reply_text("I didn't understand that. Can you try typing your answer?")
        return AWAITING_INFO

    prompt = f"""
    Analyze the user's following description. Extract two pieces of information:
    1. An estimation of the carbohydrates (in grams) from the meal description.
    2. The number of insulin units injected.

    To help you, here are examples of carbs for reference:
    "One cup of cooked white rice" : 45  
    "Medium baked potato with skin" : 37  
    "1 bowl of spaghetti with tomato sauce" : 55  
    "Half a cup of cooked brown rice" : 22  
    "Grilled chicken breast, no sauce" : 0  
    "Slice of pizza with pepperoni" : 30  

    Return ONLY a JSON object with two keys: "carbs" (integer) and "insulin_units" (integer).
    If either piece of information cannot be reliably determined, its value should be null.

    User description: "{user_input}"
    """
    
    data = await get_openai_json_response(prompt)
    carbs = data.get("carbs")
    insulin = data.get("insulin_units")

    # Validation loop: both values must be present
    if carbs is not None and insulin is not None:
        new_record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "carbs": carbs,
            "insulin": insulin
        }
        event_records.append(new_record)
        await update.message.reply_text("Perfect, thanks! The information has been recorded.")
        return ConversationHandler.END
    else:
        # Constructive error message
        missing_info = []
        if carbs is None:
            missing_info.append("what you ate")
        if insulin is None:
            missing_info.append("the insulin dose")
        
        await update.message.reply_text(
            f"I'm still missing information about { ' and '.join(missing_info) }. "
            "Can you please rephrase to include both details?"
        )
        return AWAITING_INFO # Stays in the same state to ask again

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current conversation."""
    await update.message.reply_text("Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    if 'chat_history' in context.user_data:
        del context.user_data['chat_history']
    return ConversationHandler.END

# --- Deep Dive Chat Handler ---

async def handle_deep_dive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles general text messages for conversational Q&A."""
    user_message = update.message.text
    await update.message.reply_text("Thinking...")

    if 'chat_history' not in context.user_data:
        context.user_data['chat_history'] = []
    
    log_summary = "No records on file."
    if event_records:
        log_summary = "Here are the user's past records:\n"
        for i, record in enumerate(event_records):
            log_summary += f"- Record {i+1} ({record['timestamp']}): Carbs={record['carbs']}g, Insulin={record['insulin']} units\n"

    system_prompt = (
        "You are a helpful diabetes assistant. Your role is to answer the user's questions based on their provided logs and conversation history. "
        "IMPORTANT: You are an AI assistant, NOT a medical professional. Always include a disclaimer that your advice is not a substitute for consultation with a doctor or endocrinologist. "
        "Analyze the provided records and chat history to give the most relevant, helpful, and safe response."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "system", "content": f"BACKGROUND INFO:\n{log_summary}"})
    messages.extend(context.user_data['chat_history'])
    messages.append({"role": "user", "content": user_message})

    ai_response = await get_openai_chat_response(messages)
    await update.message.reply_text(ai_response)

    context.user_data['chat_history'].append({"role": "user", "content": user_message})
    context.user_data['chat_history'].append({"role": "assistant", "content": ai_response})
    context.user_data['chat_history'] = context.user_data['chat_history'][-10:]

# --- Utility Functions and Commands ---

async def _get_text_from_message(message) -> Union[str, None]:
    if message.text:
        return message.text
    if message.voice:
        return await transcribe_voice(message.voice)
    return None

async def transcribe_voice(voice_message) -> Union[str, None]:
    """Downloads, converts, and transcribes a voice message."""
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
            # Changed language to English
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
        "Hi! I'm Mori. I may text you from time to time 👋\n\n"
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
    if 'chat_history' in context.user_data:
        del context.user_data['chat_history']
        await update.message.reply_text("Our current conversation history has been cleared.")
    else:
        await update.message.reply_text("There's no conversation history to clear.")

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN environment variable is not set.")
        return

    application = Application.builder().token(token).build()

    record_handler = ConversationHandler(
        entry_points=[CommandHandler("trigger_alert", trigger_alert)],
        states={
            AWAITING_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE, received_info)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    deep_dive_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_deep_dive)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("show_records", show_records))
    application.add_handler(CommandHandler("clear_chat", clear_chat))
    
    application.add_handler(record_handler)
    application.add_handler(deep_dive_handler)
    
    print("Bot started with the combined question. Press Ctrl+C to stop.")
    application.run_polling()


if __name__ == "__main__":
    main()
