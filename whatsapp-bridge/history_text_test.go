package main

import (
	"testing"

	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func TestHandleHistorySync_PreservesVisibleText(t *testing.T) {
	const text = "synthetic history text"
	cases := []struct {
		name string
		msg  *waProto.Message
	}{
		{"conversation", &waProto.Message{Conversation: proto.String(text)}},
		{"extended_text", &waProto.Message{ExtendedTextMessage: &waProto.ExtendedTextMessage{Text: proto.String(text)}}},
		{"business_template", &waProto.Message{TemplateMessage: &waProto.TemplateMessage{
			HydratedTemplate: &waProto.TemplateMessage_HydratedFourRowTemplate{HydratedContentText: proto.String(text)},
		}}},
		{"buttons", &waProto.Message{ButtonsMessage: &waProto.ButtonsMessage{ContentText: proto.String(text)}}},
		{"image_caption", &waProto.Message{ImageMessage: &waProto.ImageMessage{Caption: proto.String(text)}}},
		{"video_caption", &waProto.Message{VideoMessage: &waProto.VideoMessage{Caption: proto.String(text)}}},
		{"document_caption", &waProto.Message{DocumentMessage: &waProto.DocumentMessage{Caption: proto.String(text)}}},
	}
	for _, conversation := range []struct {
		name     string
		archived bool
	}{{"active", false}, {"archived", true}} {
		t.Run(conversation.name, func(t *testing.T) {
			for _, tc := range cases {
				t.Run(tc.name, func(t *testing.T) {
					ms := newTestMessageStore(t)
					client := newTestClientWithSelf(&mockLIDStore{}, selfPhone)
					chatJID := phonePN.String()
					historySync := &events.HistorySync{Data: &waProto.HistorySync{
						SyncType: waProto.HistorySync_RECENT.Enum(),
						Conversations: []*waProto.Conversation{{
							ID:       proto.String(chatJID),
							Archived: proto.Bool(conversation.archived),
							Messages: []*waProto.HistorySyncMsg{{Message: &waProto.WebMessageInfo{
								Key: &waCommon.MessageKey{
									ID:     proto.String("history-text-001"),
									FromMe: proto.Bool(false),
								},
								MessageTimestamp: proto.Uint64(1700000000),
								Message:          tc.msg,
							}}},
						}},
					}}

					handleHistorySync(client, ms, historySync, testLogger())

					var count int
					var content string
					if err := ms.db.QueryRow(
						"SELECT count(*), COALESCE(max(content), '') FROM messages WHERE chat_jid = ?",
						chatJID,
					).Scan(&count, &content); err != nil {
						t.Fatal(err)
					}
					if count != 1 || content != text {
						t.Fatalf("stored rows = %d, content = %q; want one row containing %q", count, content, text)
					}
				})
			}
		})
	}
}
