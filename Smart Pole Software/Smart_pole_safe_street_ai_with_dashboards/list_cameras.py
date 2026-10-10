import cv2

# Finds which camera index is which (laptop webcam, DroidCam virtual camera...).
# Each working camera opens in a window. Press any key to go to the next one.

found = []
for idx in range(0, 6):
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
    ok, frame = (False, None)
    if cap.isOpened():
        # some virtual cameras need a few reads to warm up
        for _ in range(15):
            ok, frame = cap.read()
            if ok and frame is not None:
                break
    if ok and frame is not None:
        h, w = frame.shape[:2]
        print(f"INDEX {idx}: WORKING ({w}x{h})")
        found.append(idx)
        cv2.putText(frame, f"INDEX {idx}", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
        cv2.imshow(f"camera index {idx}", frame)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    else:
        print(f"INDEX {idx}: not available")
    cap.release()

print("\nWorking indexes:", found)
print("Window mein jo laptop ka face dikhe wo CAM01, jo phone ka view dikhe wo CAM02.")