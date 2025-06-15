import logging
import os
import json
import speech_recognition as sr
from datetime import datetime
from pydub import AudioSegment
from typing import Union, Dict

# Importation de la bibliothèque OpenAI
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

# --- Configuration de base ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialisation du client OpenAI
# La bibliothèque lira automatiquement la variable d'environnement OPENAI_API_KEY
try:
    client = openai.OpenAI()
except openai.OpenAIError:
    logger.error("Erreur: La variable d'environnement OPENAI_API_KEY n'est pas configurée.")
    exit()


# --- États de la conversation ---
ASKING_FOOD, CONFIRMING_CARBS, ASKING_INSULIN = range(3)

# --- Stockage en mémoire ---
event_records: list[Dict] = []
user_chat_id: int = None


# --- Fonctions de l'API OpenAI ---

async def get_openai_response(prompt: str) -> dict:
    """
    Envoie une requête à l'API OpenAI et attend une réponse JSON.
    """
    try:
        response = client.chat.completions.create(
            # Utilisation d'un modèle puissant et récent comme gpt-4o
            model="gpt-4o",
            # Activation du mode JSON pour une réponse fiable
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a helpful assistant designed to output JSON."},
                {"role": "user", "content": prompt}
            ]
        )
        content = response.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        logger.error(f"Erreur lors de l'appel à l'API OpenAI: {e}")
        return {}


# --- Gestionnaires de la conversation ---

async def trigger_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Simule une anomalie et démarre la conversation en demandant ce que l'utilisateur a mangé."""
    global user_chat_id
    current_chat_id = update.message.chat_id
    if user_chat_id is None:
        user_chat_id = current_chat_id
    
    logger.info(f"Déclenchement d'une alerte pour le chat_id: {user_chat_id}")

    await context.bot.send_message(
        chat_id=user_chat_id,
        text=(
            "Hi, I've noticed a recent event and have a couple of questions.\n\n"
            "First, what did you eat for your last meal?\n\n"
            "You can reply with text or a voice message.\n"
            "Send /cancel to stop."
        ),
    )
    return ASKING_FOOD

async def received_food(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Traite la description du repas pour estimer les glucides avec l'IA et passe directement à la demande d'insuline."""
    user_input = ""
    await update.message.reply_text("Thanks! Let me analyze that...")
    if update.message.text:
        user_input = update.message.text
    elif update.message.voice:
        user_input = await transcribe_voice(update.message.voice)
        if not user_input:
            await update.message.reply_text(
                "Sorry, I couldn't understand the audio. Could you please type out what you ate?\n"
                "Send /cancel to stop."
            )
            return ASKING_FOOD

    prompt = f"""
    Analyze the following food description and estimate the total carbohydrates in grams.
    Provide the answer ONLY as a JSON object with a single key "carbs" which is an integer.
    If you cannot determine a number, return a JSON with "carbs": null.
    Food description: "{user_input}"
    """
    
    data = await get_openai_response(prompt)
    print(f"Received data from OpenAI: {data}")  # Debugging line to see the response structure
    carbs = data.get("carbs")

    if carbs is not None and isinstance(carbs, int):
        context.user_data['carbs'] = carbs
        await update.message.reply_text(
            f"I estimate that meal was about {carbs}g of carbs.\n\nHow many units of insulin did you inject?",
            reply_markup=ReplyKeyboardRemove()
        )
        return ASKING_INSULIN
    else:
        await update.message.reply_text(
            "I had trouble estimating the carbs from that description. "
            "Could you please tell me the carb amount in grams directly?"
        )
        return ASKING_FOOD # Pour la simplicité, on redémarre le cycle.

async def received_insulin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit et valide la quantité d'insuline avec l'IA."""
    user_input = ""
    await update.message.reply_text("Checking the value...")
    if update.message.text:
        user_input = update.message.text
    elif update.message.voice:
        user_input = await transcribe_voice(update.message.voice)
        if not user_input:
            await update.message.reply_text("Sorry, I couldn't understand that. Please type the amount.")
            return ASKING_INSULIN

    prompt = f"""
    Analyze the following user input, which is a response to 'How many units of insulin did you inject?'.
    Extract the numerical value. The value should be a reasonable integer between 0 and 100.
    Provide the answer ONLY as a JSON object with a single key "insulin_units".
    If the input is not a reasonable number (e.g., 'blue', 'a million', negative), the value should be null.
    User input: "{user_input}"
    """
    
    data = await get_openai_response(prompt)
    insulin = data.get("insulin_units")

    if insulin is not None and isinstance(insulin, int):
        context.user_data['insulin'] = insulin
        
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
        context.user_data.clear()
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "That doesn't seem like a valid amount. Please provide a simple number for the insulin units."
        )
        return ASKING_INSULIN # Boucle pour redemander

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Annule la conversation en cours."""
    await update.message.reply_text("Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

# --- Fonctions utilitaires et autres commandes ---

async def transcribe_voice(voice_message) -> Union[str, None]:
    """Télécharge, convertit et transcrit un message vocal."""
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
            # Transcription en anglais
            text = recognizer.recognize_google(audio_data, language="en-US")
            return text
    except Exception as e:
        logger.error(f"Erreur de transcription: {e}")
        return None
    finally:
        if os.path.exists(oga_path): os.remove(oga_path)
        if os.path.exists(wav_path): os.remove(wav_path)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Message de bienvenue et configuration initiale."""
    global user_chat_id
    user_chat_id = update.message.chat_id
    logger.info(f"L'utilisateur avec le chat_id: {user_chat_id} a démarré le bot.")
    
    await update.message.reply_text(
        "Hi! I'm your insulin monitoring assistant. 👋\n\n"
        "I will contact you automatically if I detect any anomalies.\n\n"
    )

async def show_records(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche tous les enregistrements sauvegardés."""
    if not event_records:
        await update.message.reply_text("There are no records yet.")
        return
    
    message = "--- Stored Records ---\n\n"
    for i, record in enumerate(event_records):
        message += (
            f"📝 **Record #{i + 1}**\n"
            f"  - **Timestamp:** {record['timestamp']}\n"
            f"  - **Estimated Carbs:** {record['carbs']} g\n"
            f"  - **Insulin:** {record['insulin']} units\n\n"
        )
    await update.message.reply_text(message, parse_mode='Markdown')

def main() -> None:
    """Lance le bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Erreur: La variable d'environnement TELEGRAM_BOT_TOKEN n'est pas configurée.")
        return

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("trigger_alert", trigger_alert)],
        states={
            ASKING_FOOD: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE, received_food)],
            ASKING_INSULIN: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE, received_insulin)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("show_records", show_records))
    
    print("Le bot est démarré avec l'intégration OpenAI. Appuyez sur Ctrl+C pour l'arrêter.")
    application.run_polling()


if __name__ == "__main__":
    main()
