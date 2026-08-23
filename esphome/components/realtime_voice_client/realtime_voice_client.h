#pragma once

#include "esphome/components/microphone/microphone_source.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/core/component.h"
#include "esp_websocket_client.h"

#include <string>

namespace esphome::realtime_voice_client {

class RealtimeVoiceClient : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  void set_microphone_source(microphone::MicrophoneSource *source) { this->microphone_ = source; }
  void set_speaker(speaker::Speaker *speaker) { this->speaker_ = speaker; }
  void set_url(const std::string &url) { this->url_ = url; }
  void set_token(const std::string &token) { this->token_ = token; }
  void set_client_id(const std::string &client_id) { this->client_id_ = client_id; }
  void set_name(const std::string &name) { this->name_ = name; }

  void start_turn();
  void stop_turn();
  void cancel();
  bool is_connected() const { return this->connected_; }
  bool is_listening() const { return this->listening_; }
  const std::string &phase() const { return this->phase_; }

 protected:
  static void websocket_event_(void *args, esp_event_base_t base, int32_t event_id, void *data);
  void handle_event_(int32_t event_id, esp_websocket_event_data_t *event);
  void send_text_(const std::string &message);
  void set_phase_(const char *phase);

  microphone::MicrophoneSource *microphone_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  esp_websocket_client_handle_t websocket_{nullptr};
  std::string url_;
  std::string token_;
  std::string client_id_;
  std::string name_;
  std::string phase_{"disconnected"};
  bool connected_{false};
  bool listening_{false};
  bool speech_detected_{false};
  bool stop_requested_{false};
  uint32_t turn_started_ms_{0};
  uint32_t last_speech_ms_{0};
};

}  // namespace esphome::realtime_voice_client
