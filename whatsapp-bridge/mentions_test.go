package main

import (
	"testing"

	"go.mau.fi/whatsmeow/types"
)

func testParticipants() []types.GroupParticipant {
	return []types.GroupParticipant{
		{
			JID:         types.NewJID("111222333444555", types.HiddenUserServer),
			LID:         types.NewJID("111222333444555", types.HiddenUserServer),
			PhoneNumber: types.NewJID("84901234567", types.DefaultUserServer),
		},
		{
			JID:         types.NewJID("84907654321", types.DefaultUserServer),
			PhoneNumber: types.NewJID("84907654321", types.DefaultUserServer),
		},
	}
}

func TestApplyGroupMentions_RewritesPhoneToLIDAndListsJID(t *testing.T) {
	text, mentioned, err := applyGroupMentions("@84901234567 check giúp", []string{"84901234567"}, testParticipants())
	if err != nil {
		t.Fatal(err)
	}
	if text != "@111222333444555 check giúp" {
		t.Fatalf("text = %q", text)
	}
	if len(mentioned) != 1 || mentioned[0] != "111222333444555@lid" {
		t.Fatalf("mentioned = %v", mentioned)
	}
}

func TestApplyGroupMentions_PhoneAddressedMemberAndPlusPrefix(t *testing.T) {
	text, mentioned, err := applyGroupMentions("ok @+84907654321", []string{"+84907654321"}, testParticipants())
	if err != nil {
		t.Fatal(err)
	}
	if text != "ok @84907654321" || mentioned[0] != "84907654321@s.whatsapp.net" {
		t.Fatalf("text=%q mentioned=%v", text, mentioned)
	}
}

func TestApplyGroupMentions_FailsClosed(t *testing.T) {
	cases := map[string][]string{
		"not a member":  {"84900000000"},
		"missing token": {"84907654321"},
		"bad input":     {"Tú"},
	}
	for name, m := range cases {
		msg := "@84900000000 hi"
		if name == "missing token" {
			msg = "hi there"
		}
		if _, _, err := applyGroupMentions(msg, m, testParticipants()); err == nil {
			t.Fatalf("%s: expected error", name)
		}
	}
}

func TestApplyGroupMentions_TokenNotMatchedInsideLongerNumber(t *testing.T) {
	if _, _, err := applyGroupMentions("@849076543210 hi", []string{"84907654321"}, testParticipants()); err == nil {
		t.Fatal("expected error: @84907654321 is only a prefix of another number")
	}
}
