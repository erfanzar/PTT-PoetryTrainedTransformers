if __name__ == "__main__":
    v = []
    with open('q&a.txt', 'r') as stream:
        data = stream.readlines()
        for i, line in enumerate(data):
            data_list = line.split('\t')
            v.append(data_list[0]+'\n')
            v.append(data_list[1])

    with open('q&a_cleaned.txt', 'w') as stream:
        stream.writelines(v)
