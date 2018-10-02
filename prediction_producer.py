import json
import socket
import time
from multiprocessing import Pool

import cv2
import face_recognition
import tensorflow as tf
from kafka import KafkaConsumer, KafkaProducer
from keras.models import load_model

from params import *
from utils import get_model_proto
from utils import np_from_json, np_to_json


def consumer(consumer_number):
    """CONSUME video frames, predictions Published to respective camera topics
    Args:
        consumer_number: consumer number
    """
    # Declare unique client name
    iam = "{}-{}".format(socket.gethostname(), consumer_number)
    print("[INFO] I am ", iam)

    # KAFKA TODO: Check kafka compression, multiple consumer, threads safe producer

    # Connect to kafka, Consume frame obj bytes deserialize to json
    frame_consumer = KafkaConsumer(FRAME_TOPIC, group_id='predict', client_id=iam,
                                   bootstrap_servers=['0.0.0.0:9092'],
                                   key_deserializer=lambda key: key.decode(),
                                   value_deserializer=lambda value: json.loads(value.decode()))

    #  connect to Kafka, produces prediction object
    prediction_producer = KafkaProducer(bootstrap_servers=['localhost:9092'],
                                        key_serializer=lambda key: str(key).encode(),
                                        value_serializer=lambda value: json.dumps(value).encode())

    # Connect to kafka, Consume known face object to know what faces are the target
    known_faces_consumer = KafkaConsumer(KNOWN_FACE_TOPIC, group_id=str(socket.gethostname()), client_id=iam,
                                         bootstrap_servers=['0.0.0.0:9092'],
                                         value_deserializer=lambda value: json.loads(value.decode()))

    if DL == "mnist":
        model = load_model(model_path)
        graph = tf.get_default_graph()
        print(model.summary())
        print("**Model Loaded from: {}".format(model_path))

    elif DL != "tracking":

        # load our serialized model from disk
        print("[INFO] loading model...")

        model = cv2.dnn.readNetFromCaffe(proto_path, model_path)

        print("[INFO] Loaded...")

    if DL == "image_classification":
        # LABEL NAMES
        label_names = np.loadtxt(LABEL_PATH, str, delimiter='\t')

    def get_face_object(frame_obj, known_faces_data):
        """Processes value produced by producer, returns prediction with png image."""

        frame = np_from_json(frame_obj, prefix_name=ORIGINAL_PREFIX)  # frame_obj = json
        frame = cv2.cvtColor(frame.astype(np.uint8), cv2.COLOR_BGR2RGB)
        known_face_encodings = np_from_json(known_faces_data, prefix_name="known_face_encodings").tolist()  # (n, 128)
        known_face_names = np_from_json(known_faces_data, prefix_name="known_face_names").tolist()  # (n, )

        # Find all the faces and face encodings in the current frame of video
        face_locations = face_recognition.face_locations(frame)
        face_encodings = face_recognition.face_encodings(frame, face_locations)

        # faces found in this image
        face_names = []
        for face_encoding in face_encodings:
            # See if the face is a match for the known face(s)
            matches = face_recognition.compare_faces(known_face_encodings, face_encoding)
            name = "Unknown"

            # If a match was found in known_face_encodings, just use the first one.
            if True in matches:
                first_match_index = matches.index(True)
                name = known_face_names[first_match_index]

            face_names.append(name.title())

        # SAVE the results for this frame
        for (top, right, bottom, left), name in zip(face_locations, face_names):
            # Draw a box around the face
            cv2.rectangle(frame, (left, top), (right, bottom), (0, 0, 255), 2)

            # Draw a label with a name below the face
            cv2.rectangle(frame, (left, bottom - 27), (right, bottom), (0, 0, 255), cv2.FILLED)
            font = cv2.FONT_HERSHEY_SIMPLEX
            cv2.putText(frame, name, (left + 6, bottom - 6), font, 1.0, (255, 255, 255), 1)

        # frame = cv2.resize(frame, (150, 150))
        frame = cv2.cvtColor(frame.astype(np.uint8), cv2.COLOR_BGR2RGB)
        frame_dict = np_to_json(frame, prefix_name=PREDICTED_PREFIX)
        prediction = None
        if face_names:
            prediction = face_names[0]

        result = {"prediction": prediction,
                  "predict_time": str(time.time()),
                  "latency": str(time.time() - int(frame_obj['timestamp']))}

        frame_obj.update(frame_dict)  # update frame with prediction

        result.update(frame_obj)

        return result

    def get_classification_object(frame_obj):
        """Processes value produced by producer, returns prediction with png image."""

        frame = np_from_json(frame_obj, prefix_name=ORIGINAL_PREFIX)  # frame_obj = json
        # This CNN requires fixed spatial dimensions for our input image(s)
        # so we need to ensure it is resized to 224x224 pixels while
        # performing mean subtraction (104, 117, 123) to normalize the input;
        # after executing this command our "blob" now has the shape:
        # (1, 3, 224, 224)
        # blob = cv2.dnn.blobFromImage(frame, 1, (224, 224), (104, 117, 123))
        # MOBILE NET
        blob = cv2.dnn.blobFromImage(frame, 0.017, (224, 224), (103.94, 116.78, 123.68), swapRB=True)

        # pass the blob through the network and obtain the detections and
        # predictions
        model.setInput(blob)
        pred_start = time.time()
        predictions = model.forward()
        print("Prediction time: ", time.time() - pred_start)

        # MOBILE NET
        predictions = np.squeeze(predictions)
        idx = np.argsort(-predictions)
        label_name = None

        for i in range(5):
            label = idx[i]
            label_name = label_names[label]
            confidence = predictions[label]
            # print('%.2f - %s' % (confidence, label_name))
            if i == 0 and confidence > CONFIDENCE:
                # TODO: DISPLAY IF ITS LABEL OF INTEREST
                text = "Detected: {}, {:.2f}%".format(label_name,
                                                      confidence * 100)
                cv2.putText(frame, text, (5, 25), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, (0, 0, 255), 2)
                break

        # frame = cv2.resize(frame, (150, 150))
        frame_dict = np_to_json(frame.astype(np.uint8), prefix_name=PREDICTED_PREFIX)

        result = {"prediction": str(label_name),
                  "predict_time": str(time.time()),
                  "latency": str(time.time() - int(frame_obj['timestamp']))}

        frame_obj.update(frame_dict)  # update frame with prediction

        result.update(frame_obj)

        return result

    def plot_box(detections, frame, confidence, i, h, w):
        """Plot a box on the frame"""
        idx = int(detections[0, 0, i, 1])
        box = detections[0, 0, i, 3:7] * np.array([w, h, w, h])
        (startX, startY, endX, endY) = box.astype("int")

        # draw the prediction on the frame
        label = "{}: {:.2f}%".format(CLASSES[idx],
                                     confidence * 100)

        cv2.rectangle(frame, (startX, startY), (endX, endY),
                      COLORS[idx], 2)
        y = startY - 15 if startY - 15 > 15 else startY + 15

        cv2.putText(frame, label, (startX, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLORS[idx], 2)

        return frame, label

    def get_detection_object(frame_obj):
        """Processes value produced by producer, returns prediction with png image."""
        frame = np_from_json(frame_obj, prefix_name=ORIGINAL_PREFIX)  # frame_obj = json

        # grab the frame dimensions and convert it to a blob
        (h, w) = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(cv2.resize(frame, (300, 300)),
                                     0.007843, (300, 300), 127.5)
        # pass the blob through the network and obtain the detections and
        # predictions
        model.setInput(blob)
        pred_start = time.time()
        detections = model.forward()
        print("Prediction time: ", time.time() - pred_start)

        model_out = None
        max_confidence = 0

        # loop over the detections
        for i in np.arange(0, detections.shape[2]):
            # extract the confidence (i.e., probability) associated with
            # the prediction
            confidence = detections[0, 0, i, 2]

            # filter out weak detections by ensuring the `confidence` is
            # greater than the minimum confidence
            if confidence > CONFIDENCE:
                # extract the index of the class label from the
                # `detections`, then compute the (x, y)-coordinates of
                # the bounding box for the object

                frame, label = plot_box(detections, frame, confidence, i, h, w)

                if confidence > max_confidence:
                    model_out = label
                    max_confidence = confidence

        # frame = cv2.resize(frame, (150, 150))
        frame_dict = np_to_json(frame.astype(np.uint8), prefix_name=PREDICTED_PREFIX)

        result = {"prediction": str(model_out),
                  "predict_time": str(time.time()),
                  "latency": str(time.time() - int(frame_obj['timestamp']))}

        frame_obj.update(frame_dict)  # update frame with boundaries

        result.update(frame_obj)

        return result

    def get_mnist_object(frame_obj):
        """Processes value produced by producer, returns prediction with png image."""

        frame = np_from_json(frame_obj, prefix_name=ORIGINAL_PREFIX)  # frame_obj = json

        # MNIST SPECIFIC
        frame = frame.reshape(28, 28, 1)

        # batch
        model_in = np.expand_dims(frame, axis=0)

        # predict
        with graph.as_default():
            pred_start = time.time()
            model_out = np.argmax(np.squeeze(model.predict(model_in)))
            print("Prediction time: ", time.time() - pred_start)

        # TODO: DISPLAY IF ITS LABEL OF INTEREST
        text = "{}".format(model_out)

        # RESIZE FOR VIEWING ON FLASK
        frame = cv2.resize(frame, (90, 90))

        cv2.putText(frame, text, (5, 25), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 1)

        # frame = cv2.resize(frame, (150, 150))
        frame_dict = np_to_json(frame.astype(np.uint8), prefix_name=PREDICTED_PREFIX)

        result = {"prediction": str(model_out),
                  "predict_time": str(time.time()),
                  "latency": str(time.time() - int(frame_obj['timestamp']))}

        frame_obj.update(frame_dict)  # update frame with boundaries

        result.update(frame_obj)

        return result

    def process_stream(msg_stream, known_faces_message=None):
        try:
            null_count = 0
            while True:
                try:
                    print("[CONSUMER {}] WAITING FOR NEXT FRAME..".format(socket.gethostname()))
                    msg = next(msg_stream)

                    if not msg:
                        null_count += 1
                        print(null_count)

                    if DL == "tracking":
                        result = get_face_object(msg.value, known_faces_message.value)
                    elif DL == "object_detection":
                        result = get_detection_object(msg.value)
                    elif DL == "image_classification":
                        result = get_classification_object(msg.value)
                    elif DL == "mnist":
                        result = get_mnist_object(msg.value)
                    else:
                        print("WRONG [DL] option, check params.py, options = \
                          mnist/object_detection/image_classification ")
                        break

                    print("timestamp: {}, frame_num: {},camera_num: {}, latency: {}, y_hat: {}".format(
                        result['timestamp'],
                        result['frame_num'],
                        result['camera'],
                        result['latency'],
                        result['prediction']
                    ))

                    # camera specific topic
                    prediction_topic = "{}_{}".format(PREDICTION_TOPIC_PREFIX, result['camera'])

                    prediction_producer.send(prediction_topic, key=result['frame_num'], value=result)

                except StopIteration as excep:
                    print(excep)
                    continue

        except KeyboardInterrupt as e:
            print(e)
            pass

        finally:
            print("Closing Stream")
            msg_stream.close()

    if DL == "tracking":
        print("[CONSUMER {}] WAITING FOR TRACKING INFO..".format(socket.gethostname()))
        broadcast_msg = next(known_faces_consumer)
        print("[CONSUMER {}] GOT TRACKING INFO..".format(socket.gethostname()))
        process_stream(frame_consumer, broadcast_msg)

    else:
        process_stream(frame_consumer)

    return True


if __name__ == '__main__':
    # check or get model from s3--> cloud front --> download
    # specific DL model
    print("Objective: ", DL)
    if DL != "tracking":
        model_path, proto_path, _ = get_model_proto(target=DL)

    THREADS = 2 if SET_PARTITIONS == 8 else 1
    NUMBERS = [i for i in range(THREADS)]

    consumer_pool = Pool(THREADS)
    try:
        statuses = consumer_pool.map(consumer, NUMBERS)
        consumer_pool.close()  # close pool
        consumer_pool.join()  # wait to join
    except KeyboardInterrupt as e:
        print(e)
        consumer_pool.terminate()
        print("Done....")
