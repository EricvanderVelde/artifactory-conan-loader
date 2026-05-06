cmake_minimum_required(VERSION 3.16)
project(protobuf_demo CXX)

find_package(protobuf CONFIG REQUIRED)

add_executable(protobuf_demo src/main.cpp)

# Compile addressbook.proto → addressbook.pb.cc / addressbook.pb.h
# protobuf_generate is provided by protobuf-generate.cmake (a build module
# included automatically via find_package above).
protobuf_generate(
    TARGET        protobuf_demo
    LANGUAGE      cpp
    PROTOS        proto/addressbook.proto
    PROTOC_OUT_DIR "${CMAKE_CURRENT_BINARY_DIR}"
)

# Generated headers land in the binary dir (protobuf_generate preserves subdir structure)
target_include_directories(protobuf_demo PRIVATE "${CMAKE_CURRENT_BINARY_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/proto")
target_link_libraries(protobuf_demo PRIVATE protobuf::libprotobuf)
