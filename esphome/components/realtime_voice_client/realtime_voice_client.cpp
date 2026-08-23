#include "realtime_voice_client.h"

#include "esphome/components/audio/audio.h"
#include "esphome/core/log.h"

#include <cstdlib>

namespace esphome::realtime_voice_client {

static const char *const TAG = "realtime_voice_client";

void RealtimeVoiceClient::setup() {
  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 1, 24000));
  this->microphone_->add_data_callback([this](const std::vector<uint8_t> &data) {
    if (!this->listening_ || !this->connected_ || data.empty())
      return;
    esp_websocket_client_send_bin(this->websocket_, reinterpret_cast<const char *>(data.data()), data.size(), 0);
    const int16_t *samples = reinterpret_cast<const int16_t *>(data.data());
    const size_t sample_count = data.size() / sizeof(int16_t);
    uint64_t magnitude = 0;
    for (size_t i = 0; i < sample_count; i++)
      magnitude += std::abs(static_cast<int32_t>(samples[i]));
    if (sample_count != 0 && magnitude / sample_count > 600) {
      this->speech_detected_ = true;
      this->last_speech_ms_ = millis();
    } else if (this->speech_detected_ && millis() - this->last_speech_ms_ > 900) {
      this->stop_requested_ = true;
    }
  });

  esp_websocket_client_config_t config{};
  config.uri = this->url_.c_str();
  std::string headers = "Authorization: Bearer " + this->token_ + "\r\n";
  config.headers = headers.c_str();
  this->websocket_ = esp_websocket_client_init(&config);
  if (this->websocket_ == nullptr) {
    this->mark_failed();
    return;
  }
  esp_websocket_register_events(this->websocket_, WEBSOCKET_EVENT_ANY, &RealtimeVoiceClient::websocket_event_, this);
  esp_websocket_client_start(this->websocket_);
}

void RealtimeVoiceClient::loop() {
  if (this->stop_requested_ || (this->listening_ && millis() - this->turn_started_ms_ > 15000)) {
    this->stop_requested_ = false;
    this->stop_turn();
  }
}

void RealtimeVoiceClient::dump_config() {
  ESP_LOGCONFIG(TAG, "Realtime Voice PE client:");
  ESP_LOGCONFIG(TAG, "  URL: %s", this->url_.c_str());
  ESP_LOGCONFIG(TAG, "  Client ID: %s", this->client_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Connected: %s", YESNO(this->connected_));
}

void RealtimeVoiceClient::start_turn() {
  if (!this->connected_ || this->listening_)
    return;
  this->send_text_("{\"type\":\"ptt_start\"}");
  this->listening_ = true;
  this->speech_detected_ = false;
  this->stop_requested_ = false;
  this->turn_started_ms_ = millis();
  this->last_speech_ms_ = this->turn_started_ms_;
  this->microphone_->start();
  this->set_phase_("listening");
}

void RealtimeVoiceClient::stop_turn() {
  if (!this->listening_)
    return;
  this->microphone_->stop();
  this->listening_ = false;
  this->send_text_("{\"type\":\"ptt_stop\"}");
  this->set_phase_("thinking");
}

void RealtimeVoiceClient::cancel() {
  if (this->listening_)
    this->microphone_->stop();
  this->listening_ = false;
  this->speaker_->stop();
  if (this->connected_)
    this->send_text_("{\"type\":\"cancel\"}");
  this->set_phase_(this->connected_ ? "ready" : "disconnected");
}

void RealtimeVoiceClient::websocket_event_(void *args, esp_event_base_t, int32_t event_id, void *data) {
  static_cast<RealtimeVoiceClient *>(args)->handle_event_(event_id, static_cast<esp_websocket_event_data_t *>(data));
}

void RealtimeVoiceClient::handle_event_(int32_t event_id, esp_websocket_event_data_t *event) {
  if (event_id == WEBSOCKET_EVENT_CONNECTED) {
    this->connected_ = true;
    std::string hello = "{\"type\":\"hello\",\"protocol\":1,\"client_type\":\"voice_pe\",\"client_id\":\"" +
                        this->client_id_ + "\",\"name\":\"" + this->name_ + "\"}";
    this->send_text_(hello);
    this->set_phase_("connecting");
  } else if (event_id == WEBSOCKET_EVENT_DISCONNECTED || event_id == WEBSOCKET_EVENT_ERROR) {
    this->connected_ = false;
    this->listening_ = false;
    this->microphone_->stop();
    this->set_phase_("disconnected");
  } else if (event_id == WEBSOCKET_EVENT_DATA && event->op_code == 0x2) {
    this->speaker_->play(reinterpret_cast<const uint8_t *>(event->data_ptr), event->data_len);
    this->set_phase_("speaking");
  } else if (event_id == WEBSOCKET_EVENT_DATA && event->op_code == 0x1) {
    std::string message(event->data_ptr, event->data_len);
    if (message.find("\"type\": \"session_ready\"") != std::string::npos ||
        message.find("\"type\":\"session_ready\"") != std::string::npos ||
        message.find("\"type\": \"response.done\"") != std::string::npos ||
        message.find("\"type\":\"response.done\"") != std::string::npos)
      this->set_phase_("ready");
    else if (message.find("response.function_call") != std::string::npos)
      this->set_phase_("tool");
    else if (message.find("response.created") != std::string::npos)
      this->set_phase_("thinking");
    else if (message.find("\"type\": \"error\"") != std::string::npos ||
             message.find("\"type\":\"error\"") != std::string::npos)
      this->set_phase_("error");
  }
}

void RealtimeVoiceClient::send_text_(const std::string &message) {
  esp_websocket_client_send_text(this->websocket_, message.c_str(), message.size(), 0);
}

void RealtimeVoiceClient::set_phase_(const char *phase) { this->phase_ = phase; }

}  // namespace esphome::realtime_voice_client
