def list_dirs():
    print("\n")
    dirs_list = dirs

    for dir in dirs_list:
        print(dir)


def tree_dirs():
    print("\n")
    dirs_list = dirs

    for dir in dirs_list:
        dirs_content = os.listdir(dir)
        print(dirs_content)
