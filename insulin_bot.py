import logging
import os
import speech_recognition as sr
from datetime import datetime
from pydub import AudioSegment
from typing import Union, Dict

from telegram import Update
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

# Conversation states
ASKING_CARBS, ASKING_INSULIN = range(2)

# In-memory storage for the records and the user's chat ID
event_records: list[Dict] = []
user_chat_id: int = None


# --- Conversation Handlers ---

async def trigger_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Simulates an anomaly detection. This is the entry point for the conversation.
    The bot proactively sends a message to the stored user chat_id.
    """
    global user_chat_id
    # In a real app, you might have multiple users. For this demo, we use one.
    # The user must have typed /start at least once.
    current_chat_id = update.message.chat_id
    if user_chat_id is None:
        user_chat_id = current_chat_id # Store it if it's the first time
    
    logger.info(f"Triggering alert for chat_id: {user_chat_id}")

    await context.bot.send_message(
        chat_id=user_chat_id,
        text=(
            "Hi, I've noticed a recent event and need some details.\n\n"
            "How many carbs (in grams) did you have in your last meal?\n\n"
            "You can reply with text or a voice message.\n"
            "Send /cancel to stop."
        ),
    )
    return ASKING_CARBS

async def received_carbs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the carb amount and asks for the insulin dose."""
    user_input = ""
    if update.message.text:
        user_input = update.message.text
    elif update.message.voice:
        await update.message.reply_text("Analyzing your voice message...")
        user_input = await transcribe_voice(update.message.voice)
        if not user_input:
            await update.message.reply_text(
                "Sorry, I couldn't understand the audio. Could you please type it instead?\n"
                "Send /cancel to stop."
            )
            return ASKING_CARBS  # Remain in the same state

    # Temporarily store the response in the user's context
    context.user_data['carbs'] = user_input
    
    await update.message.reply_text(
        f"Got it. Carbs: {user_input}.\n\n"
        "Now, how many units of insulin did you inject?"
    )
    return ASKING_INSULIN

async def received_insulin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the insulin, finalizes the record, and ends the conversation."""
    user_input = ""
    if update.message.text:
        user_input = update.message.text
    elif update.message.voice:
        await update.message.reply_text("Analyzing your voice message...")
        user_input = await transcribe_voice(update.message.voice)
        if not user_input:
            await update.message.reply_text(
                "Sorry, I couldn't understand the audio. Could you please type it instead?\n"
                "Send /cancel to stop."
            )
            return ASKING_INSULIN # Remain in the same state

    context.user_data['insulin'] = user_input
    
    # Create the final record
    new_record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "carbs": context.user_data.get('carbs', 'N/A'),
        "insulin": context.user_data.get('insulin', 'N/A')
    }
    event_records.append(new_record)
    
    await update.message.reply_text(
        "Thank you! The information has been successfully recorded.\n"
        "Use /show_records to view the history."
    )
    
    # Clean up the context data
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the ongoing conversation."""
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


# --- Utility Functions and Other Commands ---

async def transcribe_voice(voice_message) -> Union[str, None]:
    """Downloads, converts, and transcribes a voice message."""
    try:
        voice_file = await voice_message.get_file()
        # Use a unique filename to avoid conflicts, though not strictly necessary here
        oga_path = f"voice_{voice_message.file_unique_id}.oga"
        wav_path = f"voice_{voice_message.file_unique_id}.wav"
        await voice_file.download_to_drive(oga_path)
        
        # Convert from OGA (Opus) to WAV
        audio = AudioSegment.from_ogg(oga_path)
        audio.export(wav_path, format="wav")
        
        # Transcribe using Google's Speech Recognition
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            # Changed language to English
            text = recognizer.recognize_google(audio_data, language="en-US")
            return text
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return None
    finally:
        # Clean up temporary files
        if os.path.exists(oga_path):
            os.remove(oga_path)
        if os.path.exists(wav_path):
            os.remove(wav_path)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message and initial setup."""
    global user_chat_id
    user_chat_id = update.message.chat_id
    logger.info(f"User with chat_id: {user_chat_id} has started the bot.")
    
    await update.message.reply_text(
        "Hi! I'm your insulin monitoring assistant. 👋\n\n"
        "I will contact you automatically if I detect any anomalies.\n\n"
        "(For demo: use /trigger_alert to simulate an anomaly detection.)"
    )

async def show_records(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays all the saved records."""
    if not event_records:
        await update.message.reply_text("There are no records yet.")
        return
    
    message = "--- Stored Records ---\n\n"
    for i, record in enumerate(event_records):
        message += (
            f"📝 **Record #{i + 1}**\n"
            f"  - **Timestamp:** {record['timestamp']}\n"
            f"  - **Carbohydrates:** {record['carbs']} g\n"
            f"  - **Insulin:** {record['insulin']} units\n\n"
        )
    
    # Using MarkdownV2 requires escaping some characters, but simple Markdown is fine here.
    await update.message.reply_text(message, parse_mode='Markdown')

def main() -> None:
    """Runs the bot."""
    # IMPORTANT: Replace with your own Telegram Bot Token
    token = "8005708467:AAGAZNmlKAIjPLS6GVrzNrdZGaWUOVYKZ0E"
    application = Application.builder().token(token).build()

    # The ConversationHandler now starts with /trigger_alert and then waits for replies.
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("trigger_alert", trigger_alert)],
        states={
            # The bot has asked for carbs, now it expects a text or voice reply.
            ASKING_CARBS: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE, received_carbs)],
            # The bot has asked for insulin, now it expects a text or voice reply.
            ASKING_INSULIN: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE, received_insulin)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # If the conversation is waiting for a reply, but the user sends a new command
        per_message=False 
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("show_records", show_records))
    
    print("Bot started. Press Ctrl+C to stop.")
    application.run_polling()


if __name__ == "__main__":
    main()
