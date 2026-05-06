#include "addressbook.pb.h"

#include <google/protobuf/util/json_util.h>

#include <cassert>
#include <iostream>
#include <string>

static demo::AddressBook build_book() {
    demo::AddressBook book;

    auto* alice = book.add_people();
    alice->set_name("Alice");
    alice->set_id(1);
    alice->set_email("alice@example.com");
    auto* phone = alice->add_phones();
    phone->set_number("+1-202-555-0101");
    phone->set_type(demo::Person::MOBILE);

    auto* bob = book.add_people();
    bob->set_name("Bob");
    bob->set_id(2);
    auto* work = bob->add_phones();
    work->set_number("+1-202-555-0199");
    work->set_type(demo::Person::WORK);

    return book;
}

int main() {
    std::cout << "Protobuf version : " << GOOGLE_PROTOBUF_VERSION << "\n\n";

    // ── Build ──────────────────────────────────────────────────────────────
    demo::AddressBook book = build_book();
    std::cout << "Built AddressBook with " << book.people_size() << " people:\n";
    for (const auto& p : book.people()) {
        std::cout << "  [" << p.id() << "] " << p.name();
        if (!p.email().empty())
            std::cout << "  <" << p.email() << ">";
        for (const auto& ph : p.phones())
            std::cout << "  " << ph.number();
        std::cout << "\n";
    }

    // ── Binary round-trip ─────────────────────────────────────────────────
    std::string binary;
    book.SerializeToString(&binary);
    std::cout << "\nSerialized to " << binary.size() << " bytes\n";

    demo::AddressBook restored;
    bool ok = restored.ParseFromString(binary);
    assert(ok && "ParseFromString failed");
    assert(restored.people_size() == book.people_size());
    std::cout << "Binary round-trip OK\n";

    // ── JSON output ───────────────────────────────────────────────────────
    std::string json;
    google::protobuf::util::JsonPrintOptions opts;
    opts.add_whitespace = true;
    auto status = google::protobuf::util::MessageToJsonString(book, &json, opts);
    assert(status.ok() && "MessageToJsonString failed");
    std::cout << "\nJSON output:\n" << json;

    google::protobuf::ShutdownProtobufLibrary();
    return 0;
}
