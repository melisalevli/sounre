MESSAGES = {
    "en": {
        "not_found": "Track not found.",
        "upstream": "A music data service is unavailable. Please try again shortly.",
        "busy": "The service is busy right now. Please try again in a moment.",
        "rate_limited": "Too many requests. Please wait a little and try again.",
    },
    "tr": {
        "not_found": "Şarkı bulunamadı.",
        "upstream": "Bir müzik veri servisine ulaşılamıyor. Lütfen birazdan tekrar dene.",
        "busy": "Servis şu an yoğun. Lütfen birazdan tekrar dene.",
        "rate_limited": "Çok fazla istek. Lütfen biraz bekleyip tekrar dene.",
    },
}


def message(key: str, lang: str | None) -> str:
    return MESSAGES.get(lang or "en", MESSAGES["en"])[key]
