import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_SPEAKER

from esphome import codegen as cg
from esphome.components import esp32, microphone, speaker

CODEOWNERS = ["@Underzenith85"]
DEPENDENCIES = ["esp32", "microphone", "speaker"]
AUTO_LOAD = ["audio"]

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_CLIENT_ID = "client_id"
CONF_NAME = "name"

realtime_voice_ns = cg.esphome_ns.namespace("realtime_voice_client")
RealtimeVoiceClient = realtime_voice_ns.class_("RealtimeVoiceClient", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(RealtimeVoiceClient),
        cv.Required(CONF_URL): cv.url,
        cv.Required(CONF_TOKEN): cv.string_strict,
        cv.Required(CONF_CLIENT_ID): cv.string_strict,
        cv.Optional(CONF_NAME, default="Home Assistant Voice PE"): cv.string,
        cv.Required(CONF_MICROPHONE): microphone.microphone_source_schema(
            min_bits_per_sample=16,
            max_bits_per_sample=16,
            min_channels=1,
            max_channels=1,
        ),
        cv.Required(CONF_SPEAKER): cv.use_id(speaker.Speaker),
    }
).extend(cv.COMPONENT_SCHEMA)

FINAL_VALIDATE_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_MICROPHONE): microphone.final_validate_microphone_source_schema(
            "realtime_voice_client", sample_rate=24000
        )
    },
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    mic_source = await microphone.microphone_source_to_code(config[CONF_MICROPHONE])
    output = await cg.get_variable(config[CONF_SPEAKER])
    cg.add(var.set_microphone_source(mic_source))
    cg.add(var.set_speaker(output))
    cg.add(var.set_url(config[CONF_URL]))
    cg.add(var.set_token(config[CONF_TOKEN]))
    cg.add(var.set_client_id(config[CONF_CLIENT_ID]))
    cg.add(var.set_name(config[CONF_NAME]))
    esp32.add_idf_component(name="espressif/esp_websocket_client", ref="1.8.0")
